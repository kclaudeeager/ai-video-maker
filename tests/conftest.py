"""Fixtures shared by every test module.

The one thing every test needs is isolation from the machine it runs on. The runner
keeps its response cache and quota ledger in a single user-wide directory
(`runner.USER_CACHE_DIR`, `~/.cache/ai-video-maker`), so a test that forgot to
redirect it would read the developer's real cached provider answers — passing for
the wrong reason — or seed that cache with mock ones. Redirecting it once for the
whole session means no test can reach it, even by omission; modules that want their
own throwaway cache (`tests/unit/test_cli_m1.py`) still override it per test, and
the session value is restored afterwards.

The music index (`audio.DEFAULT_INDEX_PATH`) lives in the same user-wide directory
and is redirected for the same reason: `videomaker music scan` writes it, and a
test that forgot would clobber the developer's real one.

The library itself is redirected too, and that one is about *results* rather than
tidiness. `Settings.music_dir` defaults to `assets/music` relative to the working
directory, and `.gitignore` keeps that folder out of the repository — so it is empty
on CI and may hold anything at all on the machine of whoever is working on this. A
render test that did not name its own music directory would put the developer's own
tracks under its output and measure something nobody else can reproduce. Setting the
environment variables rather than patching means any `Settings()` picks it up, while
a test that passes `music_dir=` explicitly still wins.
"""

import pytest

from videomaker import audio as audio_module
from videomaker import runner as runner_module


@pytest.fixture(autouse=True, scope="session")
def _isolate_user_cache(tmp_path_factory):
    """Point the caches and the audio library at throwaway paths."""
    cache_dir = tmp_path_factory.mktemp("user_cache")
    library_dir = tmp_path_factory.mktemp("empty_library")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(runner_module, "USER_CACHE_DIR", cache_dir)
        patch.setattr(audio_module, "DEFAULT_INDEX_PATH", cache_dir / audio_module.INDEX_FILENAME)
        patch.setenv("MUSIC_DIR", str(library_dir / "music"))
        patch.setenv("SFX_DIR", str(library_dir / "sfx"))
        yield


# ------------------------------------------------------- measuring caption type

#: A probe size big enough that FreeType's integer metrics round to under a tenth of
#: a percent. The ratio below is read off a face once, at this size, and applied to
#: whatever the style actually asks for.
_METRIC_PROBE_PX = 1000


@pytest.fixture(scope="session")
def caption_face():
    """`style -> PIL font`, sized and weighted the way libass will draw that style.

    ASS ``Fontsize`` is a **line height**, not an em size: libass scales the face so
    that its ascender plus its descender comes to ``Fontsize`` pixels, which for
    DejaVu Sans is 0.859 em. Type set at ``Fontsize: 96`` therefore draws about 86 %
    as wide as any ordinary renderer would at "size 96", and measuring a caption at
    face value over-states every line by a sixth — enough to call a caption that fits
    its box an overflow, and enough to over-count a real one. It is why the M3 spike
    log reports 58 % and 44 % of vertical lines over the box where real libass draws
    22 %.

    Derived from the face's own metrics rather than pinned as a number, because the
    ratio is a property of the font: a caption style that ever names a different
    family would silently get the wrong one. Pinned against the real ``subtitles``
    filter, on rendered pixels and in both aspects, by
    `test_the_type_metric_matches_what_libass_actually_draws` in
    `tests/integration/test_caption_frame_fit.py`.

    The family is resolved through fontconfig twice: once by name, to learn whether
    this machine really has it (``fc-match`` never fails — it substitutes silently,
    which is how a caption font goes missing with nothing looking wrong), and once
    with ``:bold``, because every caption style sets Bold and the bold face is wider.
    """
    from PIL import ImageFont

    from videomaker.media.fonts import resolve_family

    def face(style):
        named = resolve_family(style.font_name)
        bold = resolve_family(f"{style.font_name}:bold")
        if named is None or bold is None or not named.exact:
            pytest.skip(f"fontconfig cannot resolve {style.font_name} on this machine")
        ascent, descent = ImageFont.truetype(bold.path, _METRIC_PROBE_PX).getmetrics()
        ppem = style.font_size * _METRIC_PROBE_PX / (ascent + descent)
        return ImageFont.truetype(bold.path, ppem)

    return face


# ------------------------------------------- reading a commented-out YAML block


@pytest.fixture(scope="session")
def commented_example():
    """Parse one commented-out block of `config.example.yaml`, by its key.

    Both the voice-provider and the music-source examples are shipped commented
    out and both are pinned by a test that uncomments them. Slicing to the end of
    the file was fine while there was one such block; the second one turned the
    first test's slice into the second one's *prose*, which is not YAML. So a
    block is its key line plus the indented comment lines under it — the YAML —
    and the unindented prose between blocks is skipped.
    """
    import re
    from pathlib import Path

    import yaml

    repo = Path(__file__).resolve().parents[1]

    def read(key: str) -> dict:
        lines = (repo / "config.example.yaml").read_text().splitlines()
        start = next(i for i, line in enumerate(lines) if line.startswith(f"# {key}:"))
        block = [lines[start]]
        for line in lines[start + 1 :]:
            if re.match(r"^#\s{2,}\S", line):
                block.append(line)
            elif line.strip() in {"", "#"}:
                continue
            else:
                break
        return yaml.safe_load(re.sub(r"^# ?", "", "\n".join(block), flags=re.MULTILINE))

    return read
