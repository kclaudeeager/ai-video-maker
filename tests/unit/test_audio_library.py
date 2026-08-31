"""The user's music and SFX library: the scan, the licence record, the index cache.

Three properties are load-bearing and each is tested before anything else it
depends on:

* **An empty library is the default, not an edge case.** The project ships no
  audio (`docs/audio-design.md`), so `assets/music/` is empty on every fresh
  clone including the owner's. `scan` must succeed on it, `music list` must say
  so plainly, and nothing anywhere may raise.
* **The index is a cache, never the source of truth.** `~/.cache/ai-video-maker/
  music_index.json` may be missing, stale or corrupt at any moment. The
  filesystem decides which files exist; the index only ever saves an ffprobe.
* **An unrecorded track is a warning, never a failure.** A file with no entry in
  `library.yaml` still plays; it is only reported as unattributed, because that
  record is what lets M5 generate a correct attribution block.
"""

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from videomaker import audio
from videomaker.audio import (
    Attribution,
    Library,
    Track,
    read_index,
    read_records,
    scan,
    write_index,
)
from videomaker.cli import app
from videomaker.config import Settings
from videomaker.doctor import run_checks
from videomaker.media.ffmpeg import FFmpegCaps, FFmpegError

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- helpers


def _sine(path, seconds=2.0, freq=440):
    """A real audio file, made by FFmpeg. The repo commits no audio, so tests make theirs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}", str(path)],
        check=True,
    )
    return path


def _noise(path, seconds=0.4):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"anoisesrc=d={seconds}:c=pink", str(path)],
        check=True,
    )
    return path


def _touch(path, data=b"not really audio"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class FakeProbe:
    """Stands in for ffprobe, and counts how often it was actually needed."""

    def __init__(self, duration=12.5):
        self.duration = duration
        self.calls = []

    def __call__(self, path):
        self.calls.append(path)
        return self.duration


@pytest.fixture
def dirs(tmp_path):
    music = tmp_path / "assets" / "music"
    sfx = tmp_path / "assets" / "sfx"
    music.mkdir(parents=True)
    sfx.mkdir(parents=True)
    return music, sfx


@dataclass(frozen=True)
class CliEnv:
    music: Path
    sfx: Path
    index: Path


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """A cwd-relative `assets/` tree and a throwaway index, so the CLI touches nothing real.

    `Settings.music_dir`/`sfx_dir` default to relative paths, so chdir is all the
    commands need; the index is user-wide and has to be redirected explicitly.
    """
    monkeypatch.chdir(tmp_path)
    music = tmp_path / "assets" / "music"
    sfx = tmp_path / "assets" / "sfx"
    music.mkdir(parents=True)
    sfx.mkdir(parents=True)
    index = tmp_path / "cache" / "music_index.json"
    monkeypatch.setattr(audio, "DEFAULT_INDEX_PATH", index)
    return CliEnv(music=music, sfx=sfx, index=index)


# --------------------------------------------------------------- the empty library


def test_scan_of_an_empty_tree_finds_nothing(dirs):
    music, sfx = dirs
    library = scan(music, sfx, probe=FakeProbe())

    assert library.tracks == ()
    assert not library
    assert library.warnings() == ()


def test_scan_of_a_tree_that_does_not_exist_is_still_empty_not_an_error(tmp_path):
    library = scan(tmp_path / "nope" / "music", tmp_path / "nope" / "sfx", probe=FakeProbe())

    assert library.tracks == ()
    assert library.warnings() == ()


def test_an_empty_library_reports_no_moods_and_no_roles(dirs):
    library = scan(*dirs, probe=FakeProbe())

    assert library.moods() == ()
    assert library.roles() == ()
    assert library.unattributed() == ()


def test_scan_writes_an_index_even_when_the_library_is_empty(dirs, tmp_path):
    index = tmp_path / "cache" / "music_index.json"
    write_index(scan(*dirs, probe=FakeProbe()), index)

    assert index.exists()
    assert read_index(index) == {}


def test_music_scan_on_an_empty_library_succeeds_and_says_so(cli_env):
    result = runner.invoke(app, ["music", "scan"])

    assert result.exit_code == 0
    assert "no audio" in result.output
    assert "not an error" in result.output
    assert cli_env.index.exists()


def test_music_list_on_an_empty_library_says_so_plainly(cli_env):
    result = runner.invoke(app, ["music", "list"])

    assert result.exit_code == 0
    assert "empty" in result.output
    assert "not an error" in result.output
    assert "narration only" in result.output


def test_music_list_needs_no_index_at_all(cli_env):
    """The index is a cache: `list` works with none, it does not demand a scan first."""
    assert not cli_env.index.exists()

    result = runner.invoke(app, ["music", "list"])

    assert result.exit_code == 0


# ------------------------------------------------------------------- scanning disk


def test_scan_groups_music_by_mood_and_sfx_by_role(dirs):
    music, sfx = dirs
    _touch(music / "calm" / "rain.wav")
    _touch(music / "upbeat" / "drive.mp3")
    _touch(sfx / "transition" / "whoosh.wav")

    library = scan(music, sfx, probe=FakeProbe())

    assert [t.key for t in library.tracks] == [
        "music/calm/rain.wav",
        "music/upbeat/drive.mp3",
        "sfx/transition/whoosh.wav",
    ]
    assert library.moods() == ("calm", "upbeat")
    assert library.roles() == ("transition",)
    assert [t.key for t in library.for_mood("calm")] == ["music/calm/rain.wav"]
    assert [t.key for t in library.for_role("transition")] == ["sfx/transition/whoosh.wav"]


def test_scan_ignores_everything_that_is_not_audio(dirs):
    music, sfx = dirs
    _touch(music / "README.md")
    _touch(music / "library.yaml")
    _touch(music / "calm" / ".DS_Store")
    _touch(music / "calm" / "notes.txt")
    _touch(sfx / "transition" / ".gitkeep")
    kept = _touch(music / "calm" / "rain.wav")

    library = scan(music, sfx, probe=FakeProbe())

    assert [t.path for t in library.tracks] == [kept]


def test_scan_records_the_probed_duration(dirs):
    music, sfx = dirs
    _touch(music / "calm" / "rain.wav")
    probe = FakeProbe(duration=63.25)

    library = scan(music, sfx, probe=probe)

    assert library.tracks[0].duration_s == 63.25
    assert library.probed == 1


def test_durations_come_from_real_ffprobe(dirs):
    """The one test that runs the real binary — a duration nobody hardcoded."""
    music, sfx = dirs
    _sine(music / "calm" / "tone.wav", seconds=2.0)
    _noise(sfx / "transition" / "whoosh.wav", seconds=0.4)

    library = scan(music, sfx)

    by_key = {t.key: t for t in library.tracks}
    assert by_key["music/calm/tone.wav"].duration_s == pytest.approx(2.0, abs=0.05)
    assert by_key["sfx/transition/whoosh.wav"].duration_s == pytest.approx(0.4, abs=0.05)


def test_a_file_that_ffprobe_cannot_read_is_recorded_not_raised(dirs):
    """A corrupt drop-in must not take the whole library (or a render) down with it."""
    music, sfx = dirs
    _touch(music / "calm" / "broken.wav", b"\x00\x01 not audio")

    def boom(path):
        raise FFmpegError("ffprobe exited with status 1")

    library = scan(music, sfx, probe=boom)

    assert len(library.tracks) == 1
    assert library.tracks[0].duration_s == 0.0
    assert "ffprobe" in library.tracks[0].probe_error
    assert any("could not be read" in w for w in library.warnings())


def test_probe_none_skips_probing_entirely(dirs):
    """`videomaker doctor` counts and credits files; it must not shell out per track."""
    music, sfx = dirs
    _touch(music / "calm" / "rain.wav")

    library = scan(music, sfx, probe=None)

    assert len(library.tracks) == 1
    assert library.probed == 0


# ------------------------------------------------------------- the licence record


def _write_library_yaml(music, body):
    (music / "library.yaml").write_text(body)


def test_a_record_attaches_attribution_to_its_track(dirs):
    music, sfx = dirs
    _touch(music / "calm" / "rain.wav")
    _write_library_yaml(music, """
tracks:
  music/calm/rain.wav:
    title: Rain On Glass
    artist: Kevin MacLeod
    licence: CC-BY-4.0
    attribution: '"Rain On Glass" by Kevin MacLeod (CC BY 4.0)'
    source_url: https://incompetech.com/music/
""")

    library = scan(music, sfx, probe=FakeProbe())

    track = library.tracks[0]
    assert track.attributed
    assert track.attribution.artist == "Kevin MacLeod"
    assert track.attribution.licence == "CC-BY-4.0"
    assert library.unattributed() == ()


def test_an_unrecorded_track_is_usable_but_reported_as_unattributed(dirs):
    """The warning path: a missing record never blocks anything, it only reports."""
    music, sfx = dirs
    _touch(music / "calm" / "rain.wav")
    _write_library_yaml(music, "tracks: {}\n")

    library = scan(music, sfx, probe=FakeProbe())

    assert len(library.tracks) == 1, "the track is still usable"
    assert library.tracks[0].attribution is None
    assert [t.key for t in library.unattributed()] == ["music/calm/rain.wav"]
    assert any("library.yaml" in w for w in library.warnings())


def test_no_library_yaml_at_all_is_a_warning_not_a_failure(dirs):
    music, sfx = dirs
    _touch(music / "calm" / "rain.wav")

    library = scan(music, sfx, probe=FakeProbe())

    assert len(library.tracks) == 1
    assert len(library.unattributed()) == 1


def test_a_record_key_relative_to_the_music_dir_also_matches(dirs):
    """`calm/rain.wav` is the obvious thing to type in a file that lives in `music/`."""
    music, sfx = dirs
    _touch(music / "calm" / "rain.wav")
    _write_library_yaml(music, "tracks:\n  calm/rain.wav:\n    title: Rain\n")

    library = scan(music, sfx, probe=FakeProbe())

    assert library.tracks[0].attribution.title == "Rain"


def test_a_record_can_credit_an_sfx_file_too(dirs):
    music, sfx = dirs
    _touch(sfx / "transition" / "whoosh.wav")
    _write_library_yaml(music, "tracks:\n  sfx/transition/whoosh.wav:\n    licence: CC0\n")

    library = scan(music, sfx, probe=FakeProbe())

    assert library.tracks[0].attribution.licence == "CC0"


def test_a_record_for_a_file_that_is_gone_is_reported_as_an_orphan(dirs):
    music, sfx = dirs
    _write_library_yaml(music, "tracks:\n  music/calm/deleted.wav:\n    title: Gone\n")

    library = scan(music, sfx, probe=FakeProbe())

    assert library.orphan_records == ("music/calm/deleted.wav",)
    assert any("no longer on disk" in w for w in library.warnings())


@pytest.mark.parametrize("body", ["- a\n- b\n", "just a string\n", "tracks: 7\n", "{{{\n"])
def test_a_malformed_library_yaml_warns_and_credits_nothing(dirs, body):
    music, sfx = dirs
    _touch(music / "calm" / "rain.wav")
    _write_library_yaml(music, body)

    library = scan(music, sfx, probe=FakeProbe())

    assert len(library.tracks) == 1, "the audio is still usable"
    assert library.tracks[0].attribution is None
    assert any("library.yaml" in w for w in library.warnings())


def test_a_credit_line_prefers_the_explicit_attribution_string(dirs):
    explicit = Attribution(
        title="Rain", artist="Kevin MacLeod", licence="CC-BY-4.0",
        attribution="Rain by Kevin MacLeod, licensed under CC BY 4.0",
    )
    assert explicit.credit_line() == "Rain by Kevin MacLeod, licensed under CC BY 4.0"


def test_a_credit_line_is_composed_when_no_explicit_string_was_given():
    composed = Attribution(title="Rain", artist="Kevin MacLeod", licence="CC-BY-4.0")
    line = composed.credit_line()
    assert "Rain" in line and "Kevin MacLeod" in line and "CC-BY-4.0" in line


def test_the_example_library_yaml_shipped_in_the_repo_parses_and_is_empty():
    """A fresh clone must warn about nothing: the examples in it are commented out."""
    records, warning = read_records(REPO_ROOT / "assets" / "music" / "library.yaml")

    assert records == {}
    assert warning == ""


# ----------------------------------------------- the index is a cache, not truth


def _index_for(library, path):
    write_index(library, path)
    return path


def test_a_track_deleted_since_the_index_was_written_disappears(dirs, tmp_path):
    music, sfx = dirs
    gone = _touch(music / "calm" / "rain.wav")
    index = _index_for(scan(music, sfx, probe=FakeProbe()), tmp_path / "index.json")
    gone.unlink()

    library = scan(music, sfx, index_path=index, probe=FakeProbe())

    assert library.tracks == (), "the index still lists it; the filesystem decides"


def test_a_track_added_since_the_index_was_written_still_appears(dirs, tmp_path):
    music, sfx = dirs
    index = _index_for(scan(music, sfx, probe=FakeProbe()), tmp_path / "index.json")
    _touch(music / "calm" / "new.wav")

    library = scan(music, sfx, index_path=index, probe=FakeProbe())

    assert [t.key for t in library.tracks] == ["music/calm/new.wav"]


def test_a_file_replaced_since_the_index_was_written_is_reprobed(dirs, tmp_path):
    music, sfx = dirs
    path = _touch(music / "calm" / "rain.wav", b"first")
    index = _index_for(scan(music, sfx, probe=FakeProbe(duration=10.0)), tmp_path / "index.json")
    _touch(music / "calm" / "rain.wav", b"a different file entirely")

    library = scan(music, sfx, index_path=index, probe=FakeProbe(duration=99.0))

    assert library.tracks[0].duration_s == 99.0, "the cached duration is for the old bytes"
    assert library.probed == 1
    assert path.exists()


def test_a_file_rewritten_to_the_same_size_is_still_reprobed(dirs, tmp_path):
    """Size alone is not identity — mtime is what moves on any rewrite."""
    music, sfx = dirs
    _touch(music / "calm" / "rain.wav", b"aaaaa")
    index = _index_for(scan(music, sfx, probe=FakeProbe(duration=10.0)), tmp_path / "index.json")
    _touch(music / "calm" / "rain.wav", b"bbbbb")
    os.utime(music / "calm" / "rain.wav", ns=(0, 0))  # deterministically not the indexed mtime

    library = scan(music, sfx, index_path=index, probe=FakeProbe(duration=99.0))

    assert library.tracks[0].duration_s == 99.0
    assert library.probed == 1


def test_an_unchanged_file_is_served_from_the_index_without_probing(dirs, tmp_path):
    music, sfx = dirs
    _touch(music / "calm" / "rain.wav")
    index = _index_for(scan(music, sfx, probe=FakeProbe(duration=10.0)), tmp_path / "index.json")
    probe = FakeProbe(duration=99.0)

    library = scan(music, sfx, index_path=index, probe=probe)

    assert probe.calls == [], "that is the entire point of the cache"
    assert library.tracks[0].duration_s == 10.0
    assert library.reused == 1
    assert library.probed == 0


@pytest.mark.parametrize("body", ["not json at all", "[]", '{"version": 999, "tracks": []}', ""])
def test_an_unusable_index_is_ignored_rather_than_raising(dirs, tmp_path, body):
    music, sfx = dirs
    _touch(music / "calm" / "rain.wav")
    index = tmp_path / "index.json"
    index.write_text(body)

    library = scan(music, sfx, index_path=index, probe=FakeProbe(duration=7.0))

    assert library.tracks[0].duration_s == 7.0
    assert library.probed == 1


def test_a_missing_index_just_means_everything_is_probed(dirs, tmp_path):
    music, sfx = dirs
    _touch(music / "calm" / "rain.wav")

    library = scan(music, sfx, index_path=tmp_path / "never-written.json", probe=FakeProbe())

    assert library.probed == 1
    assert read_index(tmp_path / "never-written.json") == {}


def test_the_index_round_trips_through_disk(dirs, tmp_path):
    music, sfx = dirs
    _touch(music / "calm" / "rain.wav")
    index = tmp_path / "deep" / "index.json"

    write_index(scan(music, sfx, probe=FakeProbe(duration=42.0)), index)
    cached = read_index(index)

    assert set(cached) == {"music/calm/rain.wav"}
    assert cached["music/calm/rain.wav"].duration_s == 42.0
    assert json.loads(index.read_text())["version"] == audio.INDEX_VERSION


def test_the_index_is_not_trusted_across_a_moved_library(dirs, tmp_path):
    """Keys are library-relative, so a same-named file elsewhere must be reprobed."""
    music, sfx = dirs
    _touch(music / "calm" / "rain.wav", b"first")
    index = _index_for(scan(music, sfx, probe=FakeProbe(duration=10.0)), tmp_path / "index.json")

    other = tmp_path / "elsewhere" / "music"
    _touch(other / "calm" / "rain.wav", b"a completely different track")
    library = scan(other, sfx, index_path=index, probe=FakeProbe(duration=99.0))

    assert library.tracks[0].duration_s == 99.0


# ------------------------------------------------------------------ the repo layout


def test_every_documented_mood_and_role_directory_exists():
    for mood in audio.EXAMPLE_MOODS:
        assert (REPO_ROOT / "assets" / "music" / mood).is_dir(), mood
    for role in audio.SFX_ROLES:
        assert (REPO_ROOT / "assets" / "sfx" / role).is_dir(), role


@pytest.mark.parametrize("readme", ["assets/music/README.md", "assets/sfx/README.md"])
def test_the_readmes_carry_the_approved_source_table(readme):
    text = (REPO_ROOT / readme).read_text()
    for source in ("YouTube Audio Library", "Pixabay", "Freesound", "Free Music Archive",
                   "Incompetech"):
        assert source in text, f"{readme} is missing {source}"


@pytest.mark.parametrize("readme", ["assets/music/README.md", "assets/sfx/README.md"])
def test_the_readmes_state_the_youtube_ripping_non_goal(readme):
    """The single most important sentence in either file."""
    text = (REPO_ROOT / readme).read_text().lower()
    assert "never" in text
    assert "yt-dlp" in text or "extract audio from" in text
    assert "terms of service" in text


@pytest.mark.parametrize(
    "relpath,ignored",
    [
        ("assets/music/calm/whatever.mp3", True),
        ("assets/music/upbeat/some track.wav", True),
        ("assets/sfx/transition/whoosh.wav", True),
        ("assets/music/library.yaml", False),
        ("assets/music/README.md", False),
        ("assets/sfx/README.md", False),
        ("assets/sfx/transition/.gitkeep", False),
    ],
)
def test_gitignore_keeps_audio_out_and_the_record_in(relpath, ignored):
    """A user's own tracks must never be committable by accident; the docs must."""
    result = subprocess.run(
        ["git", "check-ignore", "-q", relpath], cwd=REPO_ROOT, check=False
    )
    assert (result.returncode == 0) is ignored, relpath


# -------------------------------------------------------------------------- the CLI


def test_music_scan_writes_the_index_and_reports_what_it_found(cli_env):
    _sine(cli_env.music / "calm" / "tone.wav", seconds=1.0)
    _noise(cli_env.sfx / "transition" / "whoosh.wav", seconds=0.4)

    result = runner.invoke(app, ["music", "scan"])

    assert result.exit_code == 0
    assert "2" in result.output
    assert set(read_index(cli_env.index)) == {
        "music/calm/tone.wav",
        "sfx/transition/whoosh.wav",
    }


def test_music_list_prints_each_file_with_its_group_and_length(cli_env):
    _sine(cli_env.music / "calm" / "tone.wav", seconds=12.0)
    runner.invoke(app, ["music", "scan"])

    result = runner.invoke(app, ["music", "list"])

    assert result.exit_code == 0
    assert "tone.wav" in result.output
    assert "calm" in result.output
    assert "0:12" in result.output


@pytest.mark.parametrize(
    "seconds,shown",
    [(0.0, "?"), (-1.0, "?"), (0.5, "0.5s"), (9.94, "9.9s"), (12.0, "0:12"), (95.0, "1:35")],
)
def test_durations_read_as_lengths_not_as_zero(seconds, shown):
    """A half-second whoosh shown as `0:00` reads as broken rather than as short."""
    assert audio.format_duration(seconds) == shown


def test_music_list_flags_a_track_that_is_not_in_the_library_record(cli_env):
    _sine(cli_env.music / "calm" / "tone.wav", seconds=1.0)

    result = runner.invoke(app, ["music", "list"])

    assert result.exit_code == 0
    assert "library.yaml" in result.output


def test_music_list_does_not_write_the_index(cli_env):
    _sine(cli_env.music / "calm" / "tone.wav", seconds=1.0)

    runner.invoke(app, ["music", "list"])

    assert not cli_env.index.exists(), "`list` reads; `scan` writes"


def test_music_list_shows_a_track_the_stale_index_has_never_heard_of(cli_env):
    """The CLI must not believe the cache over the disk either."""
    _sine(cli_env.music / "calm" / "tone.wav", seconds=1.0)
    runner.invoke(app, ["music", "scan"])
    _sine(cli_env.music / "upbeat" / "later.wav", seconds=1.0)

    result = runner.invoke(app, ["music", "list"])

    assert "later.wav" in result.output


# ------------------------------------------------------------------------- doctor


CAPS = FFmpegCaps(installed=True, version="ffmpeg 6.1", has_subtitles_filter=True,
                  hw_encoder="", has_ffprobe=True)


def _audio_check(settings):
    return next(r for r in run_checks(settings, CAPS) if r.name == "audio library")


def test_doctor_reports_an_empty_library_as_ok(dirs, tmp_path):
    music, sfx = dirs
    check = _audio_check(Settings(workspace_dir=tmp_path, music_dir=music, sfx_dir=sfx))

    assert check.level == "ok"
    assert "narration" in check.detail


def test_doctor_warns_about_unattributed_tracks(dirs, tmp_path):
    music, sfx = dirs
    _touch(music / "calm" / "rain.wav")
    check = _audio_check(Settings(workspace_dir=tmp_path, music_dir=music, sfx_dir=sfx))

    assert check.level == "warn"
    assert "library.yaml" in check.fix


def test_doctor_is_happy_once_every_track_is_recorded(dirs, tmp_path):
    music, sfx = dirs
    _touch(music / "calm" / "rain.wav")
    _write_library_yaml(music, "tracks:\n  music/calm/rain.wav:\n    licence: CC0\n")

    check = _audio_check(Settings(workspace_dir=tmp_path, music_dir=music, sfx_dir=sfx))

    assert check.level == "ok"
    assert "1" in check.detail


def test_library_dataclasses_are_importable_for_task_9():
    assert Library().tracks == ()
    assert Track(key="k", kind="music", group="calm", path=REPO_ROOT).attributed is False
