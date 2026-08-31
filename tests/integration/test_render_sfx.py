"""Transition SFX and beat accents over **real** FFmpeg, measured rather than inspected.

A test asserting that the graph contains an `adelay` proves that a string was
written. So this module does what `test_render_music.py` does for the duck: it
renders real clips and reads the audio back, then compares the energy at a known cut
with the energy at the same instant in a render with an empty `assets/sfx/`.

The bands are chosen so the three layers never overlap and each can be measured on
its own in the finished mp4:

* the **narration** is pink noise below 800 Hz,
* the **music bed** is pink noise between 3 and 5 kHz (the band `test_render_music.py`
  already uses),
* the **effects** are 0.3 s bursts of pink noise between 6 and 9 kHz.

A high-pass at 5.5 kHz therefore isolates the effects, and a band-pass at 3-5 kHz
still isolates the bed — which is what lets the last test here show that adding
effects does not disturb the duck.

The narration bed is generated to exactly the length of the assembled timeline, so
the cut timestamps `scene_timeline` reports are the timestamps in the finished file
and the measurement windows can be written against them.

**No audio is committed.** Every file here is generated into `tmp_path` by ffmpeg,
because the project ships no audio and never will (`docs/audio-design.md`).
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from videomaker.cache import ResponseCache, StageCache
from videomaker.config import Settings
from videomaker.media import audio as mix
from videomaker.models import Aspect, Scene, SceneVisual, VisualKind
from videomaker.pipeline import assemble as assemble_module
from videomaker.pipeline import render as render_module
from videomaker.pipeline.align import run_align
from videomaker.pipeline.assemble import (
    narration_relpath,
    run_assemble,
    scene_timeline,
    timeline_duration_s,
)
from videomaker.pipeline.base import SCENE_GAP_S, StageDeps
from videomaker.pipeline.captions import run_captions
from videomaker.pipeline.render import output_relpath, run_render, sfx_plan
from videomaker.pipeline.visuals import run_visuals
from videomaker.pipeline.voice import run_voice
from videomaker.project import ProjectStore
from videomaker.providers.ratelimit import QuotaTracker

#: Ten words each, so a segment is 10 x 0.4 s plus the 0.5 s gap = 4.5 s and the
#: whole cut is 13.5 s. Short enough that four real encodes stay bearable.
SCENES = (
    ("s01", "hook", "A drive stores every byte inside a grid of tiny cells"),
    ("s02", "mechanism", "Each cell traps electrons behind one thin insulating layer today"),
    ("s03", "close", "That is why a solid state drive forgets almost nothing"),
)

#: 0.3 s of pink noise, 6-9 kHz. Short, like a real whoosh (`assets/sfx/README.md`).
EFFECT_S = 0.3
EFFECT_BAND = (
    "highpass=f=6000:poles=2,highpass=f=6000:poles=2,"
    "lowpass=f=9000:poles=2,lowpass=f=9000:poles=2"
)
#: Everything this passes in the finished mp4 is an effect and nothing else.
EFFECT_ONLY = "highpass=f=5500:poles=2,highpass=f=5500:poles=2"

LOW_BAND = "lowpass=f=800:poles=2,lowpass=f=800:poles=2"
MUSIC_BAND_SRC = (
    "highpass=f=3000:poles=2,highpass=f=3000:poles=2,"
    "lowpass=f=5000:poles=2,lowpass=f=5000:poles=2"
)
#: Between the narration and the effects: this passes the bed alone.
MUSIC_ONLY = (
    "highpass=f=2000:poles=2,highpass=f=2000:poles=2,"
    "lowpass=f=5200:poles=2,lowpass=f=5200:poles=2"
)

LOUDNESS_TARGET_LUFS = -14.0
WINDOW_S = 0.25


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", *args], check=True)


def _write_narration(path: Path, duration_s: float, silences) -> None:
    gates = ",".join(
        f"volume=enable='between(t,{start},{end})':volume=0" for start, end in silences
    )
    chain = f"{LOW_BAND},volume=2.4dB" + (f",{gates}" if gates else "")
    _ffmpeg(
        "-f", "lavfi",
        "-i",
        f"anoisesrc=color=pink:sample_rate=48000:duration={duration_s}:seed=7:amplitude=0.5",
        "-af", chain,
        "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "1",
        str(path),
    )


def _write_effect(path: Path, seed: int) -> None:
    """One short burst, 44.1 kHz stereo — a different rate and layout from the bed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _ffmpeg(
        "-f", "lavfi",
        "-i",
        f"anoisesrc=color=pink:sample_rate=44100:duration={EFFECT_S}:seed={seed}:amplitude=0.9",
        "-af", f"{EFFECT_BAND},volume=24dB",
        "-c:a", "pcm_s16le", "-ar", "44100", "-ac", "2",
        str(path),
    )


def _write_music(path: Path, duration_s: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _ffmpeg(
        "-f", "lavfi",
        "-i",
        f"anoisesrc=color=pink:sample_rate=44100:duration={duration_s}:seed=11:amplitude=0.7",
        "-af", f"{MUSIC_BAND_SRC},volume=21dB",
        "-c:a", "pcm_s16le", "-ar", "44100", "-ac", "2",
        str(path),
    )


# ------------------------------------------------------------------- measurement


def _ffmpeg_stderr(args: list[str]) -> str:
    return subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", *args, "-f", "null", "-"],
        capture_output=True,
        text=True,
        check=True,
    ).stderr


def band_rms_db(path: Path, window: tuple[float, float], band: str = EFFECT_ONLY) -> float:
    start, duration = window
    stderr = _ffmpeg_stderr(
        ["-ss", f"{start}", "-t", f"{duration}", "-i", str(path), "-af",
         f"{band},astats=measure_overall=none"]
    )
    for line in stderr.splitlines():
        if "RMS level dB" in line:
            return float(line.rsplit(":", 1)[1])
    raise AssertionError(f"astats reported no RMS for {path} at {window}:\n{stderr}")


def integrated_lufs(path: Path) -> float:
    stderr = _ffmpeg_stderr(["-i", str(path), "-af", "ebur128=framelog=verbose"])
    for line in reversed(stderr.splitlines()):
        if "I:" in line and "LUFS" in line:
            return float(line.split("I:")[1].replace("LUFS", "").strip())
    raise AssertionError(f"ebur128 reported no integrated loudness for {path}:\n{stderr}")


# --------------------------------------------------------------------- the renders


def _deps(tmp_path: Path) -> StageDeps:
    settings = Settings(
        workspace_dir=tmp_path / "workspace",
        music_dir=tmp_path / "assets" / "music",
        sfx_dir=tmp_path / "assets" / "sfx",
        provider_chains={"tts": ["mock"], "stt": ["mock"], "stock": ["mock"], "image": ["mock"]},
    )
    return StageDeps(
        settings=settings,
        store=ProjectStore(settings.workspace_dir),
        stage_cache=StageCache(tmp_path / "stages.json"),
        response_cache=ResponseCache(tmp_path / "responses"),
        quota=QuotaTracker(tmp_path / "quota.json"),
    )


@pytest.fixture(scope="module")
def bench(tmp_path_factory):
    """One assembly, four renders: nothing, whooshes, every role, and music too."""
    tmp_path = tmp_path_factory.mktemp("render_sfx")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(assemble_module, "ASSEMBLE_ASPECTS", (Aspect.WIDE,))
        patch.setattr(render_module, "RENDER_ASPECTS", (Aspect.WIDE,))

        deps = _deps(tmp_path)
        project = deps.store.create("how ssds work", "tech_explainer", target_minutes=1.0)
        project.scenes = [
            Scene(
                id=sid,
                narration=text,
                beat=beat,
                visual=SceneVisual(query=f"{sid} b-roll", kind=VisualKind.STOCK_PHOTO),
            )
            for sid, beat, text in SCENES
        ]
        deps.store.save(project)

        run_voice(project, deps)
        run_align(project, deps)
        run_visuals(project, deps)
        run_captions(project, deps)
        run_assemble(project, deps)

        root = deps.store.path_for(project.id)
        segments = scene_timeline(project, gap_s=SCENE_GAP_S, aspect=Aspect.WIDE)
        total = timeline_duration_s(project, Aspect.WIDE)
        # A gap in the narration late in the cut, clear of every effect window below,
        # so the duck can still be read once music and effects are both in the mix.
        gap = (segments[2].start_s + 1.5, total)
        _write_narration(root / narration_relpath(Aspect.WIDE), total, [gap])

        out = root / output_relpath(Aspect.WIDE)
        renders: dict[str, Path] = {}
        results: dict[str, object] = {}

        def render(name: str) -> None:
            results[name] = run_render(project, deps)
            renders[name] = tmp_path / f"{name}.mp4"
            shutil.copy(out, renders[name])

        # 1. Empty `assets/sfx/` and empty `assets/music/` — every fresh clone.
        render("silent")

        # 2. One whoosh, and nothing else: the transition layer on its own.
        sfx = tmp_path / "assets" / "sfx"
        _write_effect(sfx / "transition" / "whoosh.wav", seed=21)
        render("cuts")

        # 3. The beats as well: an accent on hook and close, a riser into mechanism.
        _write_effect(sfx / "accent" / "ding.wav", seed=33)
        _write_effect(sfx / "riser" / "rise.wav", seed=41)
        render("beats")

        # 4. And a music bed under all of it.
        _write_music(tmp_path / "assets" / "music" / "calm" / "long.wav", 30.0)
        project.music.track_key = "music/calm/long.wav"
        render("everything")

        yield {
            "deps": deps,
            "project": project,
            "root": root,
            "renders": renders,
            "results": results,
            "segments": segments,
            "total": total,
            "gap": gap,
            "tmp_path": tmp_path,
        }


def _cut_window(bench, index: int) -> tuple[float, float]:
    """Where the whoosh for cut `index` lands in the finished file."""
    return (bench["segments"][index].start_s - mix.TRANSITION_LEAD_S, WINDOW_S)


# ------------------------------------------------- an empty library changes nothing


#: The floor an empty library leaves in the effects band: the narration's own
#: filtered noise plus what AAC puts back above 5.5 kHz. Measured, not chosen — a
#: render with a whoosh in it sits some 35 dB above this.
EMPTY_BAND_CEILING_DB = -50.0


def test_with_no_effects_there_is_nothing_in_their_band_at_the_cuts(bench):
    silent = bench["renders"]["silent"]

    assert bench["results"]["silent"].changed is True
    assert band_rms_db(silent, _cut_window(bench, 1)) < EMPTY_BAND_CEILING_DB
    assert band_rms_db(silent, _cut_window(bench, 2)) < EMPTY_BAND_CEILING_DB


def test_an_empty_sfx_directory_plans_nothing(bench):
    """The default path, stated once against the real settings object."""
    deps = _deps(bench["tmp_path"] / "fresh")

    assert sfx_plan(bench["project"], deps, Aspect.WIDE).cues == ()


# ------------------------------------------------ **the measurement: a real cut**


def test_a_whoosh_is_audible_at_every_cut(bench):
    """The number this task exists to produce: dB at the cut, with and without."""
    for index in (1, 2):
        gain = _effect_gain_db(bench, "cuts", _cut_window(bench, index))
        assert gain > 30.0, f"cut {index} only gained {gain:.1f} dB"


def _effect_gain_db(bench, name: str, window: tuple[float, float]) -> float:
    """How much `name` adds in the effects band at `window`, over the silent render.

    Normalised against the narration's own band in the same window, because the two
    renders are not normalised to the same gain: the silent one takes M1's single
    `loudnorm` pass and lands at -15.1 LUFS, the mixed one takes two and lands on
    -14.0. That ~1 dB of level would otherwise read as an effect everywhere at once,
    which is precisely the mistake this measurement has to be immune to.
    """
    with_sfx, without = bench["renders"][name], bench["renders"]["silent"]
    effects = band_rms_db(with_sfx, window) - band_rms_db(without, window)
    narration = band_rms_db(with_sfx, window, LOW_BAND) - band_rms_db(
        without, window, LOW_BAND
    )
    return effects - narration


def test_nothing_is_added_where_there_is_no_cut(bench):
    """A layer that lifted the whole band would pass the test above for free."""
    control = (bench["segments"][0].start_s + 1.5, WINDOW_S)

    assert abs(_effect_gain_db(bench, "cuts", control)) < 3.0
    # ...while at the cut a second and a half later, the same reading is enormous.
    assert _effect_gain_db(bench, "cuts", _cut_window(bench, 1)) > 30.0


def test_the_whoosh_leads_the_cut_rather_than_following_it(bench):
    """It is loudest just before the picture changes, not after."""
    path = bench["renders"]["cuts"]
    cut_at = bench["segments"][1].start_s

    before = band_rms_db(path, (cut_at - mix.TRANSITION_LEAD_S, WINDOW_S))
    after = band_rms_db(path, (cut_at + EFFECT_S, WINDOW_S))

    assert before - after > 20.0


# ---------------------------------------------------------- beat-mapped accents


def test_the_hook_is_accented_at_the_very_start(bench):
    """Nothing is at 0 s with only a transition library; the accent puts it there."""
    window = (0.0, WINDOW_S)

    assert _effect_gain_db(bench, "beats", window) > 30.0
    assert _effect_gain_db(bench, "cuts", window) < 3.0  # no cut there, and no accent


def test_the_mechanism_is_risen_into(bench):
    """The riser lands a full second ahead of the beat it introduces."""
    window = (bench["segments"][1].start_s - mix.RISER_LEAD_S, WINDOW_S)

    assert _effect_gain_db(bench, "beats", window) > 30.0
    assert _effect_gain_db(bench, "cuts", window) < 3.0  # a full second before the cut


def test_the_beats_do_not_move_the_cuts(bench):
    """Adding accents must not disturb the whooshes already placed."""
    window = _cut_window(bench, 1)

    assert band_rms_db(bench["renders"]["beats"], window) == pytest.approx(
        band_rms_db(bench["renders"]["cuts"], window), abs=2.0
    )


# ------------------------------------------- the effects and the music coexist


def test_the_music_still_ducks_with_effects_over_it(bench):
    """SFX must not fight the duck: only the bed is sidechained."""
    path = bench["renders"]["everything"]

    under_speech = band_rms_db(path, (bench["segments"][0].start_s + 1.5, 1.0), MUSIC_ONLY)
    in_the_gap = band_rms_db(path, (bench["gap"][0] + 0.5, 1.0), MUSIC_ONLY)

    duck_db = in_the_gap - under_speech
    assert duck_db > 8.0, f"the bed only dropped {duck_db:.1f} dB under speech"


def test_the_effects_are_not_ducked_with_the_music(bench):
    """A whoosh under speech is as loud as a whoosh in a gap: it is never sidechained."""
    path = bench["renders"]["everything"]

    early = band_rms_db(path, _cut_window(bench, 1))  # narration is speaking here
    late = band_rms_db(path, _cut_window(bench, 2))  # so is it; the gap is later

    assert early == pytest.approx(late, abs=4.0)
    assert early - band_rms_db(bench["renders"]["silent"], _cut_window(bench, 1)) > 25.0


def test_the_mix_still_lands_on_the_streaming_target(bench):
    """Two-pass loudness, measured over the mix the effects are actually in."""
    for name in ("cuts", "beats", "everything"):
        measured = integrated_lufs(bench["renders"][name])
        assert measured == pytest.approx(LOUDNESS_TARGET_LUFS, abs=0.6), name


# ----------------------------------------------------------------------- the cache


def test_dropping_a_file_into_the_library_re_renders(bench):
    assert bench["results"]["cuts"].changed is True
    assert bench["results"]["beats"].changed is True


def test_a_second_render_with_the_same_library_re_encodes_nothing(bench):
    result = run_render(bench["project"], bench["deps"])

    assert result.changed is False
    assert result.skipped_units == 1
