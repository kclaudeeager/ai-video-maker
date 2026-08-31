"""The music mix, everywhere it can be decided without decoding audio.

Everything here is about the *shape* of the mix — which track is chosen, what the
filter graph says, what the fingerprint covers. **None of it proves the duck.** A
graph containing `sidechaincompress` may still be inaudible, and asserting the
string proves only that the string was written; `tests/integration/test_render_music.py`
renders a real clip and measures the reduction in dB. Read that one for the guarantee.

The first section is the one that matters most in practice: `assets/music/` is empty
on every fresh clone, so "no music" is the path almost every render takes, and it has
to be *exactly* the render M1 shipped — same arguments, same hash, no re-encode.
"""

import json
from pathlib import Path

import pytest

from videomaker.audio import Attribution, Library, Track, select_track
from videomaker.config import Settings, load_settings
from videomaker.media import audio as mix
from videomaker.models import Aspect, MusicSelection
from videomaker.pipeline.assemble import AUDIO_RATE
from videomaker.pipeline.render import (
    AUDIO_CHANNELS,
    LOUDNORM,
    _render_args,
    render_hash,
)

#: Exactly the arguments M1 rendered with, written out rather than composed from the
#: constants, so a change to any of them fails here instead of passing vacuously.
M1_WIDE_ARGS = [
    "-i", "build/video_wide.mp4",
    "-i", "build/narration_wide.wav",
    "-map", "0:v:0",
    "-map", "1:a:0",
    "-vf", "subtitles=captions/wide.ass",
    "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
    "-c:v", "libx264",
    "-preset", "veryfast",
    "-crf", "18",
    "-pix_fmt", "yuv420p",
    "-r", "30",
    "-c:a", "aac",
    "-b:a", "192k",
    "-ar", "48000",
    "-ac", "2",
    "-shortest",
    "-movflags", "+faststart",
    "output/final_wide.mp4",
]

#: The `render:wide` hash M1 computed for the fixture bytes below. Frozen so that a
#: project with no music never re-renders just because this task landed.
M1_WIDE_HASH = "fc340720a6e8bfed"


@pytest.fixture
def inputs(tmp_path):
    """Three stand-in artefacts with known bytes, for the hash assertions."""
    (tmp_path / "v.mp4").write_bytes(b"video")
    (tmp_path / "n.wav").write_bytes(b"narration")
    (tmp_path / "c.ass").write_bytes(b"captions")
    return tmp_path


def _bed(tmp_path, name="calm.wav", **kwargs):
    path = tmp_path / name
    if not path.exists():
        path.write_bytes(b"a track")
    return mix.MusicBed(path=path, key=f"music/calm/{name}", **kwargs)


def _track(key, *, duration_s=90.0, probe_error=""):
    kind, _, rest = key.partition("/")
    group = rest.split("/")[0] if "/" in rest else ""
    return Track(
        key=key,
        kind=kind,
        group=group,
        path=Path("/library") / key,
        duration_s=duration_s,
        attribution=Attribution(title=key),
        probe_error=probe_error,
    )


def _library(*keys):
    return Library(tracks=tuple(_track(key) for key in keys))


# ------------------------------------------------- an empty library changes nothing


def test_with_no_music_the_arguments_are_the_ones_m1_rendered_with(tmp_path):
    assert _render_args(tmp_path, Aspect.WIDE, burn_captions=True) == M1_WIDE_ARGS


def test_with_no_music_nothing_about_the_mix_reaches_ffmpeg(tmp_path):
    args = _render_args(tmp_path, Aspect.VERTICAL, burn_captions=False)

    assert "-filter_complex" not in args
    assert "-stream_loop" not in args
    # One simple `-af`, exactly as before: no second loudness pass, no amix.
    assert args.count("-af") == 1
    assert args[args.index("-af") + 1] == f"loudnorm={LOUDNORM}"


def test_with_no_music_the_render_hash_is_the_one_m1_computed(inputs):
    """A fresh clone must not re-encode every finished video because of this task."""
    current = render_hash(
        inputs,
        Aspect.WIDE,
        video=inputs / "v.mp4",
        narration=inputs / "n.wav",
        captions=inputs / "c.ass",
        music=None,
    )

    assert current == M1_WIDE_HASH


def test_an_empty_library_selects_nothing():
    assert select_track(Library(), mood="calm", seed="p1") is None


def test_a_library_of_sound_effects_alone_selects_nothing():
    """SFX are Task 10 and are never a bed; a library of whooshes is still no music."""
    library = _library("sfx/transition/whoosh.wav", "sfx/accent/ding.wav")

    assert select_track(library, mood="calm", seed="p1") is None


# ----------------------------------------------------------------- picking a track


def test_the_templates_mood_wins_when_it_has_tracks():
    library = _library("music/calm/rain.mp3", "music/upbeat/drums.mp3")

    chosen = select_track(library, mood="upbeat", seed="p1")

    assert chosen is not None
    assert chosen.key == "music/upbeat/drums.mp3"


def test_a_mood_with_no_tracks_falls_back_to_the_whole_library():
    """Silence would read as a bug to someone who just dropped files in."""
    library = _library("music/upbeat/drums.mp3")

    chosen = select_track(library, mood="calm", seed="p1")

    assert chosen is not None
    assert chosen.key == "music/upbeat/drums.mp3"


def test_an_explicit_track_key_outranks_the_mood():
    library = _library("music/calm/rain.mp3", "music/upbeat/drums.mp3")

    chosen = select_track(library, mood="calm", track_key="music/upbeat/drums.mp3", seed="p1")

    assert chosen is not None
    assert chosen.key == "music/upbeat/drums.mp3"


def test_a_track_key_that_is_no_longer_on_disk_falls_back_to_the_mood():
    library = _library("music/calm/rain.mp3")

    chosen = select_track(library, mood="calm", track_key="music/calm/deleted.mp3", seed="p1")

    assert chosen is not None
    assert chosen.key == "music/calm/rain.mp3"


def test_a_track_ffprobe_could_not_read_is_never_chosen():
    library = Library(
        tracks=(
            _track("music/calm/broken.mp3", probe_error="Invalid data found"),
            _track("music/calm/rain.mp3"),
        )
    )

    chosen = select_track(library, mood="calm", seed="p1")

    assert chosen is not None
    assert chosen.key == "music/calm/rain.mp3"


def test_the_same_project_always_gets_the_same_track():
    library = _library(*(f"music/calm/{n}.mp3" for n in "abcdefgh"))

    picks = {select_track(library, mood="calm", seed="proj-7").key for _ in range(5)}

    assert len(picks) == 1


def test_different_projects_do_not_all_get_the_first_track():
    library = _library(*(f"music/calm/{n}.mp3" for n in "abcdefgh"))

    picks = {select_track(library, mood="calm", seed=f"proj-{i}").key for i in range(20)}

    assert len(picks) > 1


# ------------------------------------------------------------------- the duck knob


def test_no_duck_asks_the_compressor_for_no_reduction():
    assert mix.duck_ratio(0.0) == pytest.approx(1.0)


def test_a_deeper_duck_asks_for_a_higher_ratio():
    ratios = [mix.duck_ratio(depth) for depth in (3.0, 6.0, 12.0, 18.0)]

    assert ratios == sorted(ratios)
    assert len(set(ratios)) == len(ratios)


def test_the_ratio_stays_inside_what_the_filter_accepts():
    """`sidechaincompress` takes 1..20; an absurd request must clamp, not fail."""
    assert mix.duck_ratio(1000.0) == pytest.approx(mix.MAX_RATIO)
    assert mix.duck_ratio(-5.0) == pytest.approx(1.0)


def test_the_default_duck_asks_for_roughly_its_own_depth():
    """The ratio is derived from a nominal speech level, so it should land near it."""
    ratio = mix.duck_ratio(mix.DEFAULT_DUCK_DB)
    span = mix.NOMINAL_SPEECH_DBFS - mix.DUCK_THRESHOLD_DB

    assert (1 - 1 / ratio) * span == pytest.approx(mix.DEFAULT_DUCK_DB, abs=0.5)


# ------------------------------------------------------------------ the mix graph


def _graph(tmp_path, *, duration_s=10.0, measured=None, print_json=False, **bed_kwargs):
    return mix.mix_filter_complex(
        _bed(tmp_path, **bed_kwargs),
        duration_s=duration_s,
        rate=AUDIO_RATE,
        channels=AUDIO_CHANNELS,
        target=LOUDNORM,
        speech=1,
        music=2,
        measured=measured,
        print_json=print_json,
    )


def test_the_music_is_resampled_at_mix_time(tmp_path):
    """M1 follow-up 5: the narration chain is one rate, the user's track is another."""
    graph = _graph(tmp_path)

    music_chain = next(part for part in graph.split(";") if part.startswith("[2:a]"))
    assert f"aresample={AUDIO_RATE}" in music_chain
    assert "channel_layouts=stereo" in music_chain


def test_the_speech_feeds_both_the_mix_and_the_sidechain(tmp_path):
    graph = _graph(tmp_path)

    assert "[1:a]" in graph
    assert "asplit=2[sc][voice]" in graph
    assert "[bed][sc]sidechaincompress=" in graph
    assert "amix=inputs=2:normalize=0" in graph


def test_the_bed_is_trimmed_and_faded_to_the_length_of_the_cut(tmp_path):
    graph = _graph(tmp_path, duration_s=40.0)
    fade_in, fade_out = mix.fade_window(40.0)

    assert "atrim=end=40.000" in graph
    assert f"afade=t=in:st=0:d={fade_in:.3f}" in graph
    assert f"afade=t=out:st={40.0 - fade_out:.3f}:d={fade_out:.3f}" in graph


def test_the_fades_never_swallow_a_short_cut(tmp_path):
    fade_in, fade_out = mix.fade_window(2.0)

    assert 0 < fade_in <= 0.5
    assert 0 < fade_out <= 0.5


def test_the_tracks_path_never_reaches_the_filter_graph(tmp_path):
    """M0's rule: a path in a filter argument breaks the parser on the first colon."""
    bed = _bed(tmp_path, name="a: track.mp3")

    graph = mix.mix_filter_complex(
        bed,
        duration_s=10.0,
        rate=AUDIO_RATE,
        channels=AUDIO_CHANNELS,
        target=LOUDNORM,
        speech=1,
        music=2,
    )

    assert str(bed.path) not in graph
    assert "a: track.mp3" not in graph
    assert mix.music_input_args(bed) == ["-stream_loop", "-1", "-i", str(bed.path)]


def test_the_bed_volume_is_the_one_asked_for(tmp_path):
    graph = _graph(tmp_path, volume_db=-24.0)

    assert "volume=-24.00dB" in graph


# ------------------------------------------------------------- two-pass loudness


MEASURED_JSON = """
[Parsed_loudnorm_10 @ 0x60b310729fc0]
{
\t"input_i" : "-23.29",
\t"input_tp" : "-19.82",
\t"input_lra" : "2.40",
\t"input_thresh" : "-33.34",
\t"output_i" : "-13.52",
\t"output_tp" : "-9.65",
\t"output_lra" : "2.90",
\t"output_thresh" : "-23.59",
\t"normalization_type" : "dynamic",
\t"target_offset" : "-0.48"
}
"""


def test_the_first_pass_asks_loudnorm_to_print_its_measurement(tmp_path):
    graph = _graph(tmp_path, print_json=True)

    assert "print_format=json" in graph
    assert "measured_I" not in graph


def test_the_measurement_is_read_back_off_ffmpegs_stderr():
    measured = mix.parse_loudness(MEASURED_JSON)

    assert measured is not None
    assert measured.input_i == pytest.approx(-23.29)
    assert measured.input_tp == pytest.approx(-19.82)
    assert measured.input_lra == pytest.approx(2.40)
    assert measured.input_thresh == pytest.approx(-33.34)
    assert measured.offset == pytest.approx(-0.48)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "ffmpeg exited before it printed anything",
        "{not json at all}",
        json.dumps({"input_i": "-23.0"}),  # truncated: no threshold, no offset
        MEASURED_JSON.replace('"-23.29"', '"-inf"'),  # silence measures as -inf
    ],
    ids=["empty", "no-json", "garbage", "truncated", "silence"],
)
def test_an_unusable_measurement_is_no_measurement(text):
    """Pass one is an optimisation; anything unreadable falls back to a single pass."""
    assert mix.parse_loudness(text) is None


def test_the_second_pass_feeds_the_measurement_back_in(tmp_path):
    measured = mix.parse_loudness(MEASURED_JSON)

    graph = _graph(tmp_path, measured=measured)

    assert f"loudnorm={LOUDNORM}" in graph
    assert "measured_I=-23.29" in graph
    assert "measured_TP=-19.82" in graph
    assert "measured_LRA=2.40" in graph
    assert "measured_thresh=-33.34" in graph
    assert "offset=-0.48" in graph
    assert "linear=true" in graph
    assert "print_format=json" not in graph


def test_the_measuring_pass_decodes_no_video(tmp_path):
    """It exists to be cheap: narration and music in, nothing out."""
    args = mix.measure_args(
        _bed(tmp_path),
        narration="build/narration_wide.wav",
        duration_s=10.0,
        rate=AUDIO_RATE,
        channels=AUDIO_CHANNELS,
        target=LOUDNORM,
    )

    assert args[:2] == ["-i", "build/narration_wide.wav"]
    assert args[-5:] == ["-map", "[aout]", "-f", "null", "-"]
    assert "print_format=json" in args[args.index("-filter_complex") + 1]
    assert "build/video_wide.mp4" not in args


# -------------------------------------------------------- the render arguments


def test_the_music_becomes_a_third_input_that_loops(tmp_path):
    bed = _bed(tmp_path)
    plan = mix.MusicMix(bed=bed, duration_s=12.0)

    args = _render_args(tmp_path, Aspect.WIDE, burn_captions=True, mix=plan)

    assert args[:8] == [
        "-i", "build/video_wide.mp4",
        "-i", "build/narration_wide.wav",
        "-stream_loop", "-1",
        "-i", str(bed.path),
    ]
    assert "-map" in args and args[args.index("-map") + 1] == "0:v:0"
    assert "[aout]" in args
    # The video is still burned by a simple filtergraph; only the audio is complex.
    assert args[args.index("-vf") + 1] == "subtitles=captions/wide.ass"
    assert "-af" not in args


def test_the_mixed_render_keeps_every_encoder_setting(tmp_path):
    plan = mix.MusicMix(bed=_bed(tmp_path), duration_s=12.0)

    args = _render_args(tmp_path, Aspect.WIDE, burn_captions=False, mix=plan)

    for flag, value in (("-c:v", "libx264"), ("-crf", "18"), ("-ar", "48000"), ("-ac", "2")):
        assert args[args.index(flag) + 1] == value
    assert args[-1] == "output/final_wide.mp4"


# ------------------------------------------------------------- the fingerprint


def _hash(inputs, bed):
    return render_hash(
        inputs,
        Aspect.WIDE,
        video=inputs / "v.mp4",
        narration=inputs / "n.wav",
        captions=inputs / "c.ass",
        music=bed,
    )


def test_adding_music_changes_the_render_fingerprint(inputs):
    assert _hash(inputs, _bed(inputs)) != M1_WIDE_HASH


def test_changing_the_track_changes_the_render_fingerprint(inputs):
    first = _hash(inputs, _bed(inputs, name="one.wav"))
    (inputs / "two.wav").write_bytes(b"a different track entirely")

    assert _hash(inputs, _bed(inputs, name="two.wav")) != first


def test_replacing_the_files_bytes_changes_the_render_fingerprint(inputs):
    """Same name, different music: the hash is over content, like every other input."""
    before = _hash(inputs, _bed(inputs, name="one.wav"))
    (inputs / "one.wav").write_bytes(b"re-downloaded, and not the same take")

    assert _hash(inputs, _bed(inputs, name="one.wav")) != before


def test_turning_the_bed_down_changes_the_render_fingerprint(inputs):
    quiet = _hash(inputs, _bed(inputs, volume_db=-24.0))
    loud = _hash(inputs, _bed(inputs, volume_db=-12.0))

    assert quiet != loud


def test_ducking_harder_changes_the_render_fingerprint(inputs):
    shallow = _hash(inputs, _bed(inputs, duck_db=6.0))
    deep = _hash(inputs, _bed(inputs, duck_db=18.0))

    assert shallow != deep


# ------------------------------------------------------------------ the settings


def test_the_project_starts_with_no_opinion_about_music():
    selection = MusicSelection()

    assert selection.enabled is True
    assert selection.track_key == ""
    assert selection.mood == ""
    assert selection.volume_db is None
    assert selection.duck_db is None


def test_a_project_written_before_this_task_still_loads():
    from datetime import UTC, datetime

    from videomaker.models import Project

    project = Project(id="p1", topic="t", template="tech_explainer", created_at=datetime.now(UTC))

    assert project.music == MusicSelection()


def test_the_mix_defaults_come_from_the_settings():
    settings = Settings()

    assert settings.music_volume_db == mix.DEFAULT_VOLUME_DB
    assert settings.duck_amount_db == mix.DEFAULT_DUCK_DB


def test_config_yaml_can_move_the_bed_and_the_duck(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("audio:\n  music_volume_db: -24\n  duck_amount_db: 9\n")

    settings = load_settings(path)

    assert settings.music_volume_db == pytest.approx(-24.0)
    assert settings.duck_amount_db == pytest.approx(9.0)


def test_the_render_mixes_at_the_rate_the_narration_bed_was_written_at(tmp_path):
    """The narration bed decides the rate; the user's 44.1 kHz track follows it."""
    plan = mix.MusicMix(bed=_bed(tmp_path), duration_s=12.0)

    args = _render_args(tmp_path, Aspect.WIDE, burn_captions=False, mix=plan)

    graph = args[args.index("-filter_complex") + 1]
    assert graph.count(f"aresample={AUDIO_RATE}") == 2  # the narration and the bed
    assert args[args.index("-ar") + 1] == str(AUDIO_RATE)
