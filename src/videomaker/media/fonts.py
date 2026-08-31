"""Can this machine actually *draw* the languages we offer?

**Measured, not assumed.** The M3 plan expected the caption font's own character
map to be the limit: `CaptionStyle.font_name` is `DejaVu Sans`, DejaVu has no
Han and no Devanagari, so Chinese captions were expected to burn in as tofu. A
frame rendered through the real `subtitles` filter says otherwise — libass asks
fontconfig for a font covering each glyph the styled face lacks, and Chinese,
Japanese and Devanagari all drew correctly with `Fontname: DejaVu Sans` in the
style line. See `docs/language-support.md` for the frame.

So the question this module answers is libass's question, not DejaVu's:

> is there **any** installed font that can draw this character?

which is what `fc-list :charset=…` answers, and what the union of every
installed font's charset answers in one call instead of one call per codepoint.

**One subprocess per process.** `fc-list --format='%{charset}'` dumps roughly
3 MB across a couple of thousand fonts in ~140 ms. The create form asks about a
few dozen characters on every render, so the dump is read once, coalesced into
sorted ranges, and memoised. Failures are not
memoised — a box that gains fontconfig should not have to restart the server.

**Fail open, and report.** With no fontconfig there is nothing to probe *and*
libass has no fallback of its own, so a machine that cannot answer keeps the
languages the round-trip measurement cleared and `doctor` says the probe never
ran. Silently offering nothing would be a worse lie than offering five.
"""

import subprocess
from bisect import bisect_right
from dataclasses import dataclass
from functools import cache

from videomaker.languages import LANGUAGES, OFFERED_CODES
from videomaker.media.ass import DEFAULT_FONT

#: The family the caption styles name. Read from `media.ass` so there is one
#: place that decides what libass is asked for.
CAPTION_FONT = DEFAULT_FONT

FC_LIST = "fc-list"
FC_MATCH = "fc-match"

#: How long either fontconfig tool gets. Both are local index reads; a hang is a
#: broken box, and the create form must not wait on one.
PROBE_TIMEOUT_S = 10.0

#: Named in the doctor check's `fix`. Debian/Ubuntu package names, because that
#: is the primary platform (global constraint) — the check says so in words.
FONT_PACKAGE_FIX = (
    "install the fonts for that script — Debian/Ubuntu: "
    "'sudo apt install fonts-dejavu-core fonts-noto-core'; macOS: "
    "'brew install --cask font-noto-sans'"
)

#: The top of Unicode. Used as the sentinel high end of a bisect probe key.
MAX_CODEPOINT = 0x10FFFF

#: Characters that are laid out rather than drawn. A font missing them is not a
#: font that produces tofu, so they are never reported.
_NOT_DRAWN = frozenset(" \t\r\n ")


@dataclass(frozen=True)
class ResolvedFamily:
    """What fontconfig would hand libass when asked for a family by name."""

    #: The family fontconfig actually returned.
    family: str
    #: The file it lives in.
    path: str
    #: True when `family` is the one that was asked for. fontconfig always
    #: answers *something*, so this is the only way to tell "installed" from
    #: "quietly substituted".
    exact: bool


@dataclass(frozen=True)
class ScriptCheck:
    """One language's script, and whether anything installed can draw it."""

    code: str
    name: str
    #: The characters from the language's sample that nothing can draw. Empty
    #: when the script is fine — and also empty when `probed` is false.
    missing: str
    #: False when there was no fontconfig to ask. `missing` then means nothing.
    probed: bool

    @property
    def renders(self) -> bool:
        return not self.missing


def clear_font_cache() -> None:
    """Forget the memoised charset union. For tests, and after installing fonts."""
    _drawable.cache_clear()


def fontconfig_charsets() -> str | None:
    """Every installed font's charset, one font per line — or None if unaskable.

    The single subprocess seam in this module: tests replace *this* rather than
    reaching into `subprocess`, so a test can describe a machine with only Latin
    fonts without owning one.
    """
    try:
        done = subprocess.run(  # fixed argv, never a shell
            [FC_LIST, "--format=%{charset}\n"],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout


def _parse_ranges(dump: str) -> list[tuple[int, int]]:
    """`20-7e a0-ff 4e2d` -> inclusive integer ranges, junk skipped."""
    ranges: list[tuple[int, int]] = []
    for token in dump.split():
        low, _, high = token.partition("-")
        try:
            start = int(low, 16)
            end = int(high, 16) if high else start
        except ValueError:
            continue
        if end >= start:
            ranges.append((start, end))
    return ranges


def _merge(ranges: list[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    """Sort and coalesce, so membership is one bisect rather than a linear scan."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return tuple(merged)


@cache
def _drawable() -> tuple[tuple[int, int], ...] | None:
    """The union of every installed font's charset, as merged ranges — or None.

    Kept as ranges rather than a set of codepoints: the union spans most of the
    BMP plus CJK, which is millions of integers, and every question asked of it
    is about one character at a time.
    """
    dump = fontconfig_charsets()
    if dump is None:
        return None
    return _merge(_parse_ranges(dump))


def _covered(ranges: tuple[tuple[int, int], ...], codepoint: int) -> bool:
    index = bisect_right(ranges, (codepoint, MAX_CODEPOINT))
    return index > 0 and ranges[index - 1][1] >= codepoint


def undrawable(text: str) -> str:
    """The characters of `text` no installed font can draw, deduplicated in order.

    Empty when everything draws — and empty when there is nothing to ask, which
    is why `ScriptCheck.probed` exists rather than overloading this return.
    """
    covered = _drawable()
    if covered is None:
        return ""
    missing: list[str] = []
    for char in text:
        if char in _NOT_DRAWN or char in missing:
            continue
        if not _covered(covered, ord(char)):
            missing.append(char)
    return "".join(missing)


def script_checks() -> list[ScriptCheck]:
    """Every registered language, in registry order, with its script probed."""
    probed = _drawable() is not None
    return [
        ScriptCheck(
            code=language.code,
            name=language.name,
            missing=undrawable(language.script_sample),
            probed=probed,
        )
        for language in LANGUAGES.values()
    ]


def renderable_codes() -> frozenset[str]:
    """Languages whose script something installed can draw. Ignores the chain gate."""
    return frozenset(check.code for check in script_checks() if check.renders)


def offerable_codes() -> frozenset[str]:
    """Both gates: measured to work end to end **and** drawable on this machine.

    This is what the create form may offer. A language fails it either because
    the round trip came back wrong (`languages.Language.note`) or because no
    installed font can draw its script (`script_checks`) — two different
    failures with two different fixes, which is why they are kept apart.
    """
    return frozenset(OFFERED_CODES) & renderable_codes()


def resolve_family(family: str) -> ResolvedFamily | None:
    """What fontconfig hands back for `family`, or None if it cannot be asked.

    `exact` is the interesting field. fontconfig never fails a match: ask for a
    family nobody has and it returns its best substitute with no error, which is
    precisely how a caption font can go missing without anything looking wrong.
    """
    try:
        done = subprocess.run(  # fixed argv, never a shell
            [FC_MATCH, "--format=%{family}\t%{file}", family],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0 or "\t" not in done.stdout:
        return None
    families, _, path = done.stdout.partition("\t")
    # fontconfig returns every alias of the matched family, comma separated.
    names = [name.strip().casefold() for name in families.split(",")]
    return ResolvedFamily(
        family=families.split(",")[0].strip(),
        path=path.strip(),
        exact=family.strip().casefold() in names,
    )
