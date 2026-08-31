"""The 480p review preview, over **real** FFmpeg.

Mocking the encode here would test nothing worth testing: the whole point of the
artefact is that FFmpeg accepts the downscaled filter graph — including the
`subtitles=` argument, whose relative path is load-bearing (an absolute `.ass` path
breaks FFmpeg's filter-graph parser the moment a folder name contains a colon, the
M0 spike's finding). So the fixtures are a genuine 1080p silent video, a genuine
narration wav and a genuine `.ass` file; only their content is trivial.

The preview is deliberately **not** a `STAGE_ORDER` stage (design decision 4), so
these tests also pin the thing that would otherwise go unnoticed: it caches itself
under its own `preview:wide` key in the same `StageCache` without appearing in the
stage order the runner and `derive_status` walk.
"""

import json
import subprocess

import pytest

from videomaker.cache import STAGE_ORDER, ResponseCache, StageCache, stage_key
from videomaker.config import Settings
from videomaker.media.ass import write_ass
from videomaker.media.audio import MusicBed, MusicMix, SfxPlan
from videomaker.media.ffmpeg import probe_dimensions, probe_duration
from videomaker.models import (
    Aspect,
    MusicSelection,
    Scene,
    SceneVisual,
    WordTiming,
)
from videomaker.pipeline.assemble import (
    VERTICAL_SPEC,
    WIDE_SPEC,
    narration_relpath,
    video_relpath,
)
from videomaker.pipeline.base import StageDeps
from videomaker.pipeline.captions import PLAY_RES, STYLES, caption_relpath
from videomaker.pipeline.render import LOUDNORM, ShortNotRenderable
from videomaker.preview import (
    PREVIEW_ASPECTS,
    PREVIEW_HEIGHT,
    _preview_args,
    build_preview,
    mix_is_current,
    preview_hash,
    preview_relpath,
    short_problem,
)
from videomaker.project import ProjectStore
from videomaker.providers.ratelimit import QuotaTracker

#: Short on purpose — the encodes are real, so every extra second is test wall time.
SOURCE_SECONDS = 2.0

WORDS = [
    WordTiming(word="Drives", start_s=0.1, end_s=0.7),
    WordTiming(word="store", start_s=0.7, end_s=1.2),
    WordTiming(word="bytes", start_s=1.2, end_s=1.8),
]


def _ffmpeg(args: list[str]) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-y", *args], check=True,
                   capture_output=True)


@pytest.fixture(scope="module")
def source_media(tmp_path_factory):
    """One real 1080p video and one real wav, encoded once for the whole module."""
    root = tmp_path_factory.mktemp("preview_source")
    video = root / "video.mp4"
    narration = root / "narration.wav"
    _ffmpeg([
        "-f", "lavfi",
        "-i", f"testsrc=size={WIDE_SPEC.width}x{WIDE_SPEC.height}:rate=30:d={SOURCE_SECONDS}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        str(video),
    ])
    # A tone, not `anullsrc`: `loudnorm` measures digital silence as -inf LUFS and
    # then hands the AAC encoder NaN. Real narration always has signal in it.
    _ffmpeg([
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", str(SOURCE_SECONDS), "-ac", "1", str(narration),
    ])
    return video, narration


def _write_captions(root, words):
    path = root / caption_relpath(Aspect.WIDE)
    path.parent.mkdir(parents=True, exist_ok=True)
    return write_ass(
        words, STYLES[Aspect.WIDE], path, play_res=PLAY_RES[Aspect.WIDE]
    )


@pytest.fixture
def assembled(tmp_path, source_media):
    """A project whose `build/` already holds what `assemble` would have left."""
    video, narration = source_media
    settings = Settings(workspace_dir=tmp_path / "workspace")
    deps = StageDeps(
        settings=settings,
        store=ProjectStore(settings.workspace_dir),
        stage_cache=StageCache(tmp_path / "stages.json"),
        response_cache=ResponseCache(tmp_path / "responses"),
        quota=QuotaTracker(tmp_path / "quota.json"),
    )
    project = deps.store.create("how ssds work", "tech_explainer", target_minutes=1.0)
    root = deps.store.path_for(project.id)
    (root / "build").mkdir(parents=True, exist_ok=True)
    (root / video_relpath(Aspect.WIDE)).write_bytes(video.read_bytes())
    (root / narration_relpath(Aspect.WIDE)).write_bytes(narration.read_bytes())
    _write_captions(root, WORDS)
    return project, deps, root


def test_preview_relpath_lives_in_build():
    assert preview_relpath(Aspect.WIDE) == "build/preview_wide.mp4"
    assert PREVIEW_HEIGHT == 480


def test_preview_is_480_high_at_the_wide_aspect_ratio(assembled):
    project, deps, root = assembled

    path = build_preview(project, deps)

    assert path == root / preview_relpath(Aspect.WIDE)
    width, height = probe_dimensions(path)
    assert height == PREVIEW_HEIGHT
    # h264 needs even dimensions, so 1920x1080 scaled to 480 high rounds up to 854.
    assert width == 854
    assert (width, height) != WIDE_SPEC.size


def test_a_second_call_with_nothing_changed_re_encodes_nothing(assembled):
    project, deps, _root = assembled

    first = build_preview(project, deps)
    stamp = first.stat().st_mtime_ns

    second = build_preview(project, deps)

    assert second == first
    assert second.stat().st_mtime_ns == stamp


def test_changing_the_captions_invalidates_the_preview(assembled):
    project, deps, root = assembled
    path = build_preview(project, deps)
    stamp = path.stat().st_mtime_ns

    _write_captions(root, [*WORDS, WordTiming(word="quickly", start_s=1.8, end_s=2.4)])

    assert build_preview(project, deps).stat().st_mtime_ns != stamp


def test_on_progress_receives_output_seconds(assembled):
    project, deps, _root = assembled
    ticks: list[float] = []

    build_preview(project, deps, on_progress=ticks.append)

    assert ticks
    assert all(tick >= 0 for tick in ticks)
    assert max(ticks) <= SOURCE_SECONDS + 1.0


def test_the_preview_is_cached_outside_the_stage_order(assembled):
    project, deps, _root = assembled

    build_preview(project, deps)

    key = stage_key("preview", Aspect.WIDE.value)
    assert key == "preview:wide"
    assert key in json.loads(deps.stage_cache.path.read_text())
    assert "preview" not in STAGE_ORDER


# ======================================================================
# M3 Task 11: the vertical proxy beside the wide one, and the mix
# ======================================================================
#
# Gate 3 shows both cuts side by side, so there are now two proxies rather than
# one. They are still outside `STAGE_ORDER` and still cached per aspect — the
# whole design decision the section above pins — and the vertical one is
# refused, not crashed, for a project with nothing in its Short.
#
# The preview also carries the *mix* now, because a music picker whose choice
# cannot be heard until after the gate is stamped is a picker nobody would use.
# The bed and the effects come out of the same functions the real render uses,
# so the two cannot drift; only the loudness pass differs (one, not two — this
# is a throwaway file, and `_plan_mix`'s own docstring says a single pass is
# what a no-music render does anyway).


@pytest.fixture(scope="module")
def portrait_media(tmp_path_factory):
    """A real 1080x1920 video, for the vertical proxy's own encode."""
    root = tmp_path_factory.mktemp("preview_portrait")
    video = root / "vertical.mp4"
    _ffmpeg([
        "-f", "lavfi",
        "-i",
        f"testsrc=size={VERTICAL_SPEC.width}x{VERTICAL_SPEC.height}:rate=30:d={SOURCE_SECONDS}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        str(video),
    ])
    return video


@pytest.fixture(scope="module")
def bed_file(tmp_path_factory):
    """A generated track. The project ships no audio, so a test may not either."""
    path = tmp_path_factory.mktemp("preview_library") / "bed.wav"
    _ffmpeg([
        "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=44100",
        "-t", str(SOURCE_SECONDS * 2), "-ac", "2", str(path),
    ])
    return path


@pytest.fixture
def assembled_short(assembled, portrait_media):
    """The wide fixture plus everything the *vertical* proxy is made from.

    One voiced scene, ticked into the Short: `check_short_limit` reads the
    vertical timeline, and a project with nothing on it is the error state this
    task has to surface rather than crash on.
    """
    project, deps, root = assembled
    (root / video_relpath(Aspect.VERTICAL)).write_bytes(portrait_media.read_bytes())
    (root / narration_relpath(Aspect.VERTICAL)).write_bytes(
        (root / narration_relpath(Aspect.WIDE)).read_bytes()
    )
    project.scenes = [
        Scene(
            id="s01",
            narration="Drives store bytes",
            duration_s=SOURCE_SECONDS,
            in_short=True,
            visual=SceneVisual(query="ssd b-roll"),
        )
    ]
    deps.store.save(project)
    return project, deps, root


# ------------------------------------------------------- the second aspect


def test_the_two_previews_are_named_and_ordered_per_aspect():
    assert preview_relpath(Aspect.VERTICAL) == "build/preview_vertical.mp4"
    assert PREVIEW_ASPECTS == (Aspect.WIDE, Aspect.VERTICAL)


def test_the_vertical_preview_is_portrait_at_480_lines(assembled_short):
    project, deps, root = assembled_short

    path = build_preview(project, deps, aspect=Aspect.VERTICAL)

    assert path == root / preview_relpath(Aspect.VERTICAL)
    width, height = probe_dimensions(path)
    assert height == PREVIEW_HEIGHT
    # 1080x1920 at `scale=-2:480` is 270x480 — portrait, not the wide 854x480.
    assert (width, height) == (270, PREVIEW_HEIGHT)


def test_each_aspect_is_cached_under_its_own_key_outside_the_stage_order(assembled_short):
    project, deps, _root = assembled_short

    wide = build_preview(project, deps, aspect=Aspect.WIDE)
    vertical = build_preview(project, deps, aspect=Aspect.VERTICAL)
    stamp = wide.stat().st_mtime_ns

    cached = json.loads(deps.stage_cache.path.read_text())
    assert stage_key("preview", "wide") in cached
    assert stage_key("preview", "vertical") in cached
    assert "preview" not in STAGE_ORDER
    # Building the Short must not re-encode the long cut.
    assert wide.stat().st_mtime_ns == stamp
    assert vertical.is_file()


def test_a_project_with_nothing_in_the_short_has_no_vertical_preview(assembled_short):
    """Task 6's error state, surfaced rather than crashed on: no file, a message."""
    project, deps, root = assembled_short
    for scene in project.scenes:
        scene.in_short = False
    deps.store.save(project)

    with pytest.raises(ShortNotRenderable, match="nothing is marked for the Short"):
        build_preview(project, deps, aspect=Aspect.VERTICAL)

    assert not (root / preview_relpath(Aspect.VERTICAL)).is_file()
    assert "nothing is marked for the Short" in short_problem(project)
    # The long cut is unaffected: an empty Short is not a broken project.
    assert short_problem(project, aspect=Aspect.WIDE) == ""
    assert build_preview(project, deps, aspect=Aspect.WIDE).is_file()


def test_a_renderable_short_reports_no_problem(assembled_short):
    project, _deps, _root = assembled_short

    assert short_problem(project) == ""


# ------------------------------------------------------------------ the mix


def test_with_no_music_the_preview_arguments_carry_nothing_about_a_mix(tmp_path):
    args = _preview_args(tmp_path, Aspect.WIDE, burn_captions=True, mix=None)

    assert "-filter_complex" not in args
    assert "-stream_loop" not in args
    assert args.count("-af") == 1
    assert args[args.index("-af") + 1] == f"loudnorm={LOUDNORM}"
    assert args[args.index("-map") + 1] == "0:v:0"


def test_with_no_music_the_preview_hash_is_the_one_it_shipped_with(assembled):
    """A fresh clone must not re-encode a preview because this task landed."""
    _project, _deps, root = assembled
    video = root / video_relpath(Aspect.WIDE)
    narration = root / narration_relpath(Aspect.WIDE)
    captions = root / caption_relpath(Aspect.WIDE)

    without = preview_hash(
        root, Aspect.WIDE, video=video, narration=narration, captions=captions
    )
    explicit_none = preview_hash(
        root,
        Aspect.WIDE,
        video=video,
        narration=narration,
        captions=captions,
        music=None,
        sfx=SfxPlan(),
    )

    assert without == explicit_none


def test_the_bed_reaches_the_preview_encode_as_an_input_not_a_filter_argument(
    tmp_path, bed_file
):
    """M0's rule: a path in a filter graph breaks the parser on a colon."""
    bed = MusicBed(path=bed_file, key="music/calm/bed.wav")
    mix = MusicMix(bed=bed, duration_s=4.0)

    args = _preview_args(tmp_path, Aspect.WIDE, burn_captions=False, mix=mix)

    assert args[args.index("-stream_loop") + 1] == "-1"
    assert args[args.index("-stream_loop") + 3] == str(bed_file)
    graph = args[args.index("-filter_complex") + 1]
    assert "sidechaincompress" in graph
    assert str(bed_file) not in graph
    assert "-af" not in args
    assert args[args.index("-map") + 1] == "0:v:0"
    assert args[args.index("-map") + 3] == "[aout]"


def test_the_preview_measures_no_loudness_pass_of_its_own(tmp_path, bed_file):
    """One pass for a throwaway file. Two is the deliverable's price, not this one's."""
    mix = MusicMix(bed=MusicBed(path=bed_file, key="music/calm/bed.wav"), duration_s=4.0)

    args = _preview_args(tmp_path, Aspect.WIDE, burn_captions=False, mix=mix)
    graph = args[args.index("-filter_complex") + 1]

    assert "measured_I" not in graph
    assert graph.count("loudnorm") == 1


def test_the_preview_hash_moves_with_the_chosen_bed(assembled, bed_file):
    _project, _deps, root = assembled
    video = root / video_relpath(Aspect.WIDE)
    narration = root / narration_relpath(Aspect.WIDE)
    common = {"video": video, "narration": narration, "captions": None}

    silent = preview_hash(root, Aspect.WIDE, **common)
    one = preview_hash(
        root, Aspect.WIDE, **common, music=MusicBed(path=bed_file, key="music/calm/one.wav")
    )
    louder = preview_hash(
        root,
        Aspect.WIDE,
        **common,
        music=MusicBed(path=bed_file, key="music/calm/one.wav", volume_db=-6.0),
    )

    assert len({silent, one, louder}) == 3


def test_a_preview_with_a_bed_really_encodes(assembled, bed_file, monkeypatch):
    """Real FFmpeg accepts the preview's complex graph, scaling and subtitles included."""
    project, deps, _root = assembled
    library = bed_file.parent
    monkeypatch.setattr(deps.settings, "music_dir", library)
    monkeypatch.setattr(deps.settings, "sfx_dir", library / "no-effects")
    project.music = MusicSelection(track_key=f"music/{bed_file.name}")
    deps.store.save(project)

    path = build_preview(project, deps)

    assert path.is_file()
    assert probe_duration(path) > 0


def test_changing_the_track_makes_the_built_preview_stale(assembled, bed_file, monkeypatch):
    """The picker's whole point: a new choice is a preview you have not heard yet."""
    project, deps, _root = assembled
    library = bed_file.parent
    monkeypatch.setattr(deps.settings, "music_dir", library)
    monkeypatch.setattr(deps.settings, "sfx_dir", library / "no-effects")
    project.music = MusicSelection(track_key=f"music/{bed_file.name}")
    deps.store.save(project)
    build_preview(project, deps)
    assert mix_is_current(project, deps, Aspect.WIDE)

    # The picker's other two knobs move the mix without moving a single byte
    # inside the project folder, which is exactly why mtime cannot answer this.
    project.music = MusicSelection(track_key=f"music/{bed_file.name}", volume_db=-6.0)
    deps.store.save(project)

    assert not mix_is_current(project, deps, Aspect.WIDE)
    build_preview(project, deps)
    assert mix_is_current(project, deps, Aspect.WIDE)
