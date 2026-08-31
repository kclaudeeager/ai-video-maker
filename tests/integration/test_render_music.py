"""The music mix over **real** FFmpeg, measured rather than inspected.

A test asserting that the filter graph contains `sidechaincompress` proves that a
string was written, not that anything was ducked. So this module renders real clips
and reads the audio back:

* The narration bed is replaced with **pink noise below 800 Hz** that speaks, falls
  silent, speaks again and stops. The music is **pink noise between 3 and 5 kHz**.
  The two never overlap in frequency, so a high-pass at 2 kHz isolates the *music* in
  the finished mp4, and its level during speech can be compared with its level in a
  gap. That difference, in dB, is the duck — the number this task exists to produce.
  Noise rather than tones because a loudness meter reading a pure tone disagrees with
  the next meter by the better part of a LU; on noise, `ebur128` and `loudnorm` agree
  to within 0.5 LU, which is what makes the loudness assertion below mean anything.
* The same measurement settles the loop (music still playing six seconds into a clip
  from a one-second track), the trim (a thirty-second track ending with the video,
  faded) and the empty library (nothing above 2 kHz at all).
* Loudness is read with `ebur128` and compared against the -14 LUFS target, which is
  what the second `loudnorm` pass is for.

Both sit where real material sits: the narration measures about -21 dBFS RMS while it
is speaking, which is what `media/audio.NOMINAL_SPEECH_DBFS` calibrates the compressor
ratio against. The music is 44.1 kHz stereo against a 48 kHz mono bed, which is the
resample this feature made matter (M1 follow-up 5).

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
from videomaker.media.ffmpeg import probe_duration
from videomaker.models import Aspect, Scene, SceneVisual, VisualKind
from videomaker.pipeline import assemble as assemble_module
from videomaker.pipeline import render as render_module
from videomaker.pipeline.align import run_align
from videomaker.pipeline.assemble import narration_relpath, run_assemble
from videomaker.pipeline.captions import run_captions
from videomaker.pipeline.render import music_bed, output_relpath, run_render
from videomaker.pipeline.visuals import run_visuals
from videomaker.pipeline.voice import run_voice
from videomaker.project import ProjectStore
from videomaker.providers.ratelimit import QuotaTracker

#: Thirteen words each, so the assembled video comfortably outlasts the 10 s bed
#: below and `-shortest` cuts on the narration rather than the picture.
SCENES = (
    (
        "s01",
        "A drive stores every byte it is given inside a grid of tiny cells",
        VisualKind.STOCK_PHOTO,
    ),
    (
        "s02",
        "Each cell traps electrons behind a thin insulator until the day it is erased",
        VisualKind.STOCK_VIDEO,
    ),
)

#: The bed: speech, gap, speech, gap. Every measurement window below is cut from it.
BED_S = 10.0
SILENCES = ((2.0, 4.0), (6.0, BED_S))

#: Where the measurements are taken, chosen against the bed and the fades.
#: `fade_window(10)` is a 1 s fade in and a 2 s fade out, so the fade out begins at 8 s.
SPEECH_WINDOW = (1.2, 0.7)  # speaking, past the fade in and the compressor's attack
GAP_WINDOW = (2.6, 1.2)  # silent, past the compressor's release
LATE_WINDOW = (6.6, 1.0)  # silent, far past a one-second track, before the fade out
TAIL_WINDOW = (9.5, 0.5)  # deep inside the fade out

#: Above the narration tone, below the music tone: everything this passes is bed.
MUSIC_BAND = "highpass=f=2000:poles=2,highpass=f=2000:poles=2"

LOUDNESS_TARGET_LUFS = -14.0
TRACK_KEY_LONG = "music/calm/long.wav"
TRACK_KEY_SHORT = "music/calm/short.wav"


# ------------------------------------------------------------------ generated audio


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", *args], check=True)


#: Two poles each, twice: enough that the narration leaves nothing measurable in the
#: music's band and vice versa, which is what makes the band measurement a clean read.
LOW_BAND = "lowpass=f=800:poles=2,lowpass=f=800:poles=2"
HIGH_BAND = (
    "highpass=f=3000:poles=2,highpass=f=3000:poles=2,"
    "lowpass=f=5000:poles=2,lowpass=f=5000:poles=2"
)


def _write_narration(path: Path) -> None:
    """Pink noise below 800 Hz that speaks, stops, speaks and stops.

    The gain lands the speaking stretches near -21 dBFS RMS — the level
    `NOMINAL_SPEECH_DBFS` calibrates the duck against — with a crest factor in the
    range real speech has, so the compressor sees something it would see in the wild.
    """
    gates = ",".join(
        f"volume=enable='between(t,{start},{end})':volume=0" for start, end in SILENCES
    )
    _ffmpeg(
        "-f", "lavfi",
        "-i", f"anoisesrc=color=pink:sample_rate=48000:duration={BED_S}:seed=7:amplitude=0.5",
        "-af", f"{LOW_BAND},volume=2.4dB,{gates}",
        "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "1",
        str(path),
    )


def _write_music(path: Path, duration_s: float) -> None:
    """Pink noise in a 3-5 kHz band, at 44.1 kHz stereo.

    A different rate *and* a different layout from the 48 kHz mono narration bed, so
    a mix that forgot to resample would not link at all.
    """
    _ffmpeg(
        "-f", "lavfi",
        "-i", f"anoisesrc=color=pink:sample_rate=44100:duration={duration_s}:seed=11:amplitude=0.7",
        "-af", f"{HIGH_BAND},volume=21dB",
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


def band_rms_db(path: Path, window: tuple[float, float]) -> float:
    """RMS of everything above 2 kHz in `window` — the music, and nothing else."""
    start, duration = window
    stderr = _ffmpeg_stderr(
        [
            "-ss", f"{start}",
            "-t", f"{duration}",
            "-i", str(path),
            "-af", f"{MUSIC_BAND},astats=measure_overall=none",
        ]
    )
    for line in stderr.splitlines():
        if "RMS level dB" in line:
            return float(line.rsplit(":", 1)[1])
    raise AssertionError(f"astats reported no RMS for {path} at {window}:\n{stderr}")


def integrated_lufs(path: Path) -> float:
    """The file's integrated loudness, as a streaming platform would measure it."""
    stderr = _ffmpeg_stderr(["-i", str(path), "-af", "ebur128=framelog=verbose"])
    for line in reversed(stderr.splitlines()):
        if "I:" in line and "LUFS" in line:
            return float(line.split("I:")[1].replace("LUFS", "").strip())
    raise AssertionError(f"ebur128 reported no integrated loudness for {path}:\n{stderr}")


# --------------------------------------------------------------------- the renders


def _deps(tmp_path: Path) -> object:
    settings = Settings(
        workspace_dir=tmp_path / "workspace",
        music_dir=tmp_path / "assets" / "music",
        sfx_dir=tmp_path / "assets" / "sfx",
        provider_chains={
            "tts": ["mock"],
            "stt": ["mock"],
            "stock": ["mock"],
            "image": ["mock"],
        },
    )
    from videomaker.pipeline.base import StageDeps

    return StageDeps(
        settings=settings,
        store=ProjectStore(settings.workspace_dir),
        stage_cache=StageCache(tmp_path / "stages.json"),
        response_cache=ResponseCache(tmp_path / "responses"),
        quota=QuotaTracker(tmp_path / "quota.json"),
    )


@pytest.fixture(scope="module")
def bench(tmp_path_factory):
    """One assembly, three renders: no music, a long track, a one-second track.

    Only the wide cut is built. The mix is aspect-independent — the same graph over
    the same narration bed — so rendering the Short too would double the wall time of
    a module that already runs real encodes, for no extra coverage.
    """
    tmp_path = tmp_path_factory.mktemp("render_music")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(assemble_module, "ASSEMBLE_ASPECTS", (Aspect.WIDE,))
        patch.setattr(render_module, "RENDER_ASPECTS", (Aspect.WIDE,))

        deps = _deps(tmp_path)
        project = deps.store.create("how ssds work", "tech_explainer", target_minutes=1.0)
        project.scenes = [
            Scene(id=sid, narration=text, visual=SceneVisual(query=f"{sid} b-roll", kind=kind))
            for sid, text, kind in SCENES
        ]
        deps.store.save(project)

        run_voice(project, deps)
        run_align(project, deps)
        run_visuals(project, deps)
        run_captions(project, deps)
        run_assemble(project, deps)

        root = deps.store.path_for(project.id)
        # The mock TTS writes real silence, which cannot duck anything. Replace the
        # finished bed with a tone that actually speaks; `assemble` has already marked
        # its unit fresh, so nothing rebuilds it underneath us.
        _write_narration(root / narration_relpath(Aspect.WIDE))

        out = root / output_relpath(Aspect.WIDE)
        results = {}
        renders = {}

        # 1. The empty library — every fresh clone's default.
        results["silent"] = run_render(project, deps)
        renders["silent"] = tmp_path / "silent.mp4"
        shutil.copy(out, renders["silent"])

        # 2. A track far longer than the video: trimmed, and faded out.
        calm = tmp_path / "assets" / "music" / "calm"
        calm.mkdir(parents=True)
        _write_music(calm / "long.wav", 30.0)
        _write_music(calm / "short.wav", 1.0)
        project.music.track_key = TRACK_KEY_LONG
        results["music"] = run_render(project, deps)
        renders["music"] = tmp_path / "music.mp4"
        shutil.copy(out, renders["music"])

        # 3. A one-second track under a ten-second video: looped.
        project.music.track_key = TRACK_KEY_SHORT
        results["looped"] = run_render(project, deps)
        renders["looped"] = tmp_path / "looped.mp4"
        shutil.copy(out, renders["looped"])

        # 4. Nothing upstream may have noticed any of that.
        results["reassemble"] = run_assemble(project, deps)

        yield {
            "deps": deps,
            "project": project,
            "root": root,
            "renders": renders,
            "results": results,
            "tmp_path": tmp_path,
        }


# ------------------------------------------------- an empty library changes nothing


def test_an_empty_library_is_not_an_error(tmp_path):
    deps = _deps(tmp_path)
    project = deps.store.create("no music here", "tech_explainer")

    assert music_bed(project, deps) is None


def test_with_no_music_the_render_carries_nothing_above_the_narration(bench):
    """The bed's tone is at 4 kHz; a narration-only render has nothing up there."""
    silent = bench["renders"]["silent"]

    assert bench["results"]["silent"].changed is True
    assert band_rms_db(silent, GAP_WINDOW) < -60.0


def test_the_music_is_unmistakably_present_once_the_library_has_a_track(bench):
    with_music = band_rms_db(bench["renders"]["music"], GAP_WINDOW)
    without = band_rms_db(bench["renders"]["silent"], GAP_WINDOW)

    assert with_music - without > 30.0


# ------------------------------------------------------------------- **the duck**


def test_the_music_ducks_measurably_under_the_narration(bench):
    """The measurement this task exists for: the bed, in dB, speech versus gap."""
    path = bench["renders"]["music"]

    under_speech = band_rms_db(path, SPEECH_WINDOW)
    in_the_gap = band_rms_db(path, GAP_WINDOW)

    duck_db = in_the_gap - under_speech
    assert duck_db > 8.0, f"the bed only dropped {duck_db:.1f} dB under speech"
    # And it is the depth that was asked for, not merely *some* reduction.
    assert duck_db == pytest.approx(mix.DEFAULT_DUCK_DB, abs=4.0)


def test_the_duck_lets_go_again_between_the_words(bench):
    """A ducker that never released would just be a quieter bed."""
    path = bench["renders"]["music"]

    assert band_rms_db(path, GAP_WINDOW) == pytest.approx(
        band_rms_db(path, LATE_WINDOW), abs=1.5
    )


# ------------------------------------------------------------- looping and trimming


def test_a_track_shorter_than_the_video_loops_to_fill_it(bench):
    """One second of music under ten seconds of video: still playing at 6.6 s."""
    late = band_rms_db(bench["renders"]["looped"], LATE_WINDOW)

    assert late - band_rms_db(bench["renders"]["silent"], LATE_WINDOW) > 30.0
    assert late == pytest.approx(band_rms_db(bench["renders"]["music"], LATE_WINDOW), abs=1.5)


def test_a_track_longer_than_the_video_ends_with_it(bench):
    """Thirty seconds of music must not make a thirty-second video."""
    duration = probe_duration(bench["renders"]["music"])

    assert duration == pytest.approx(BED_S, abs=0.2)


def test_a_track_longer_than_the_video_is_faded_out_rather_than_cut(bench):
    path = bench["renders"]["music"]

    tail = band_rms_db(path, TAIL_WINDOW)

    assert band_rms_db(path, LATE_WINDOW) - tail > 10.0


# --------------------------------------------------------------- two-pass loudness


def test_the_mix_is_measured_before_it_is_normalised(bench):
    bed = music_bed(bench["project"], bench["deps"])
    assert bed is not None

    plan = render_module._plan_mix(bed, bench["root"], Aspect.WIDE)

    assert plan is not None
    assert plan.measured is not None
    assert plan.duration_s == pytest.approx(BED_S, abs=0.05)
    assert plan.measured.input_i < 0


def test_the_mixed_render_lands_on_the_streaming_target(bench):
    """Two passes, because a single one measured -14.9 against -14 (M1 follow-up 11)."""
    measured = integrated_lufs(bench["renders"]["music"])

    assert measured == pytest.approx(LOUDNESS_TARGET_LUFS, abs=0.5)


def _mix_to_wav(bed, *, narration: Path, out: Path, measured) -> Path:
    """The very same mix graph the render uses, audio only, one pass or two."""
    _ffmpeg(
        "-i", str(narration),
        *mix.music_input_args(bed),
        "-filter_complex",
        mix.mix_filter_complex(
            bed,
            duration_s=BED_S,
            rate=48000,
            channels=2,
            target=render_module.LOUDNORM,
            speech=0,
            music=1,
            measured=measured,
        ),
        "-map", "[aout]",
        "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2",
        str(out),
    )
    return out


def test_the_second_pass_is_what_lands_it_there(bench):
    """The same mix normalised both ways — the direct case for M1 follow-up 11."""
    tmp_path = bench["tmp_path"]
    bed = mix.MusicBed(path=tmp_path / "assets" / "music" / "calm" / "long.wav", key=TRACK_KEY_LONG)
    narration = bench["root"] / narration_relpath(Aspect.WIDE)
    measured = mix.measure_loudness(
        mix.measure_args(
            bed,
            narration=str(narration),
            duration_s=BED_S,
            rate=48000,
            channels=2,
            target=render_module.LOUDNORM,
        ),
        cwd=tmp_path,
    )
    assert measured is not None

    one = integrated_lufs(
        _mix_to_wav(bed, narration=narration, out=tmp_path / "one.wav", measured=None)
    )
    two = integrated_lufs(
        _mix_to_wav(bed, narration=narration, out=tmp_path / "two.wav", measured=measured)
    )

    assert abs(two - LOUDNESS_TARGET_LUFS) < abs(one - LOUDNESS_TARGET_LUFS)
    assert two == pytest.approx(LOUDNESS_TARGET_LUFS, abs=0.5)


# ----------------------------------------------------------------------- the cache


def test_choosing_a_track_re_renders(bench):
    assert bench["results"]["music"].changed is True
    assert bench["results"]["music"].skipped_units == 0


def test_changing_the_track_re_renders(bench):
    assert bench["results"]["looped"].changed is True


def test_choosing_a_track_re_assembles_nothing(bench):
    """Music reaches the render fingerprint and stops there."""
    result = bench["results"]["reassemble"]

    assert result.changed is False
    assert result.skipped_units == len(SCENES) + 1  # every segment, plus the join


def test_a_second_render_with_the_same_track_re_encodes_nothing(bench):
    result = run_render(bench["project"], bench["deps"])

    assert result.changed is False
    assert result.skipped_units == 1
