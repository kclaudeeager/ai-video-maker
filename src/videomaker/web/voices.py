"""The voice menu the create form renders: what the TTS chain has, by language.

Two things shape this module.

**It must never take the create form down.** `KokoroTTS.voices()` raises
`ProviderConfigError` when the model weights are missing — the state of every
fresh clone, and of CI, which installs without the `ml` extra. A `<select>` built
straight from `voices()` would turn `GET /` into a 500 for anyone who has not run
`videomaker setup` yet. So every provider failure lands on `FALLBACK_VOICES`, a
short static list that always contains the form's own default, and the page says
why the menu is short instead of pretending it is complete.

**It offers only what the whole chain was measured to do.** M3 Task 21: a
language reaches the menu when the round-trip spot check cleared it
(`videomaker.languages`) *and* something installed can draw its script
(`media.fonts`). Kokoro has voices for nine languages; five survive both gates.
The rest are named in `withheld` rather than silently missing, because a menu
that is quietly short is indistinguishable from a broken one.

**Asking is expensive.** Kokoro answers `voices()` by loading a 310 MB ONNX graph
(M0 finding 2), and `get_provider` builds a fresh instance per call, so an
uncached lookup would pay that on every render of the project list. A successful
answer is therefore memoised for the life of the process. Failures are *not*
cached: they are cheap to redo (a missing-file check), and caching one would mean
a server started before `videomaker setup` kept showing the short list forever.
"""

from dataclasses import dataclass

from videomaker.config import Settings
from videomaker.languages import language_for
from videomaker.media.fonts import FONT_PACKAGE_FIX, offerable_codes, undrawable
from videomaker.providers import get_provider, resolve_chain
from videomaker.providers.errors import ProviderError
from videomaker.providers.tts.kokoro_onnx import (
    UNKNOWN_CODE,
    UNKNOWN_LANGUAGE,
    VoiceInfo,
    describe_voice,
)

#: Shown under a menu that is short because a language did not clear both gates.
#: It names the write-up rather than the reason, because the reasons differ per
#: language and the form is not where that argument belongs.
WITHHELD_NOTE = (
    "{names} not offered: the narration or its caption timing was measured wrong "
    "on this stack. See docs/language-support.md."
)

#: Offered when no provider in the chain can say what it has. Kokoro ships these
#: under any install, so they are safe to name; the first one is the create
#: form's own `DEFAULT_VOICE`, which is what makes the fallback menu usable
#: rather than merely non-empty.
FALLBACK_VOICES: tuple[str, ...] = (
    "af_heart",
    "af_sky",
    "am_adam",
    "bf_emma",
    "bm_george",
)

#: Shown under a fallback menu. It names the fix, because the fix is one command.
FALLBACK_NOTE = (
    "Showing a short built-in list: the voice catalogue could not be read "
    "({reason}). Run `videomaker setup` to install the Kokoro voices."
)

#: Where the refusal sends someone who wants the language anyway.
SUPPORT_DOC = "docs/language-support.md"

#: Successful catalogues, keyed by what could change the answer.
_CACHE: dict[tuple[str, ...], list[str]] = {}


@dataclass(frozen=True)
class VoiceGroup:
    """One `<optgroup>`: a language, and the voices that speak it."""

    language: str
    voices: list[VoiceInfo]


@dataclass(frozen=True)
class VoiceOptions:
    """The whole menu, plus whether it is the real thing."""

    groups: list[VoiceGroup]
    #: True when no provider could be asked, so `groups` is the static list.
    fallback: bool = False
    #: Why the menu is short. Empty whenever `fallback` is false.
    note: str = ""
    #: Languages the chain has voices for but this build will not offer, in the
    #: order the catalogue mentioned them. Named, not hidden.
    withheld: tuple[str, ...] = ()

    @property
    def ids(self) -> list[str]:
        """Every offered voice id, in menu order — what the template checks against."""
        return [voice.id for group in self.groups for voice in group.voices]

    @property
    def language_labels(self) -> list[str]:
        """The `<optgroup>` labels, which are also the language filter's options.

        Derived from the groups rather than from the language registry: the filter
        can then only name a language the voice menu actually has, which is half
        of why the two controls cannot disagree.
        """
        return [group.language for group in self.groups]

    @property
    def withheld_note(self) -> str:
        """One sentence naming the languages held back, or empty when none are."""
        if not self.withheld:
            return ""
        names = self.withheld[0] if len(self.withheld) == 1 else (
            ", ".join(self.withheld[:-1]) + f" and {self.withheld[-1]}"
        )
        verb = "is" if len(self.withheld) == 1 else "are"
        return WITHHELD_NOTE.format(names=f"{names} {verb}")


def refusal_for(voice_id: str) -> str:
    """Why this voice may not be used, or an empty string when it may.

    The same two gates the menu applies, said in words — because "not in the
    menu" is not a reason, and a voice posted by hand deserves the measurement
    rather than a silent video with the wrong audio. The two failures read
    differently on purpose: one is a property of the stack and one is a property
    of this machine, and only the second has a fix the reader can run.
    """
    info = describe_voice(voice_id)
    if info.code == UNKNOWN_CODE:
        # Nothing is known about it, so nothing can be held against it.
        return ""
    language = language_for(info.code)
    if language is None:
        return ""
    if not language.offered:
        return f"{language.name} is not offered: {language.note} See {SUPPORT_DOC}."
    missing = undrawable(language.script_sample)
    if missing:
        return (
            f"{language.name} captions would burn in as tofu boxes on this machine: "
            f"no installed font can draw {missing}. {FONT_PACKAGE_FIX}."
        )
    return ""


def clear_voice_cache() -> None:
    """Forget memoised catalogues. For tests, and for a process that ran `setup`."""
    _CACHE.clear()


def _cache_key(settings: Settings) -> tuple[str, ...]:
    """What a catalogue depends on: which providers are asked, and from where."""
    return (*resolve_chain("tts", settings), str(settings.models_dir))


def _query_chain(settings: Settings) -> list[str]:
    """The first TTS provider in the chain that can answer, or raise.

    Both raise sites are treated alike — a provider that cannot be built and one
    that builds but has no weights are the same thing to a form that just wants a
    list — so the walk simply moves on and only the last complaint survives.
    """
    names = resolve_chain("tts", settings)
    if not names:
        raise ProviderError("no tts provider is configured")
    problems: list[str] = []
    for name in names:
        try:
            provider = get_provider("tts", name, settings)
            return sorted(provider.voices())
        except ProviderError as exc:
            problems.append(f"{name}: {exc}")
    raise ProviderError("; ".join(problems))


def _grouped(voice_ids: list[str]) -> tuple[list[VoiceGroup], tuple[str, ...]]:
    """Voices by language, languages A-Z with `Other` last so it cannot lead.

    Returns the offerable groups and, separately, the languages dropped on the
    way. A voice whose prefix this build cannot read (`UNKNOWN_CODE`) is kept:
    Kokoro may ship a voice before the tables here learn about it, and dropping
    it would be a guess in the one direction that loses a working voice.
    """
    offerable = offerable_codes()
    by_language: dict[str, list[VoiceInfo]] = {}
    withheld: list[str] = []
    for voice_id in voice_ids:
        info = describe_voice(voice_id)
        if info.code != UNKNOWN_CODE and info.code not in offerable:
            if info.language not in withheld:
                withheld.append(info.language)
            continue
        by_language.setdefault(info.language, []).append(info)
    order = sorted(by_language, key=lambda name: (name == UNKNOWN_LANGUAGE, name))
    return (
        [VoiceGroup(language=name, voices=by_language[name]) for name in order],
        tuple(withheld),
    )


def available_voices(settings: Settings) -> VoiceOptions:
    """The voice menu for `settings`' TTS chain, grouped by language.

    Never raises: a chain that cannot answer yields the static fallback and a note
    explaining it, because the caller is a page that has to render either way.
    """
    key = _cache_key(settings)
    cached = _CACHE.get(key)
    if cached is not None:
        groups, withheld = _grouped(cached)
        return VoiceOptions(groups=groups, withheld=withheld)
    try:
        voice_ids = _query_chain(settings)
    except ProviderError as exc:
        groups, withheld = _grouped(sorted(FALLBACK_VOICES))
        return VoiceOptions(
            groups=groups,
            fallback=True,
            note=FALLBACK_NOTE.format(reason=exc),
            withheld=withheld,
        )
    _CACHE[key] = voice_ids
    groups, withheld = _grouped(voice_ids)
    return VoiceOptions(groups=groups, withheld=withheld)
