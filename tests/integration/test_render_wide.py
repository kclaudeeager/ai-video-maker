"""The whole tail of the pipeline over **real** FFmpeg, with the mock providers.

Everything upstream is offline (mock TTS writes a real 24 kHz wav, mock stock copies
the checked-in fixtures), but assemble and render run genuine encodes, so this is the
test that would catch a filter graph FFmpeg refuses, a concat list it cannot copy, or
an `.ass` path its filter-graph parser chokes on.

`test_captions_are_burned_into_the_picture` reads pixels rather than metadata on
purpose: ffprobe cannot tell a video with subtitles from the same video without them,
and a `subtitles=` filter that silently found no file still produces a playable mp4.
"""

import json
import subprocess

import pytest

from videomaker.cache import ResponseCache, StageCache
from videomaker.config import Settings
from videomaker.media.ffmpeg import probe_duration, probe_json
from videomaker.models import Aspect, Scene, SceneVisual, VisualKind
from videomaker.pipeline.align import run_align
from videomaker.pipeline.assemble import (
    WIDE_SPEC,
    concat_relpath,
    narration_relpath,
    run_assemble,
    scene_timeline,
    segment_relpath,
    timeline_relpath,
    video_relpath,
)
from videomaker.pipeline.base import SCENE_GAP_S, StageDeps
from videomaker.pipeline.captions import caption_relpath, run_captions
from videomaker.pipeline.render import output_relpath, run_render, subtitles_filter
from videomaker.pipeline.visuals import run_visuals
from videomaker.pipeline.voice import run_voice
from videomaker.project import ProjectStore
from videomaker.providers.ratelimit import QuotaTracker

#: Short on purpose: the encodes are real, so every extra second is test wall time.
SCENES = (
    ("s01", "Drives store bytes", VisualKind.STOCK_PHOTO),
    ("s02", "Cells trap electrons", VisualKind.STOCK_VIDEO),
)
SECONDS_PER_WORD = 0.4  # the mock TTS rate
DURATION_TOLERANCE_S = 0.3

#: Where the wide caption line lands: alignment 2, margin_v 160, 64 px type.
CAPTION_BAND_HEIGHT = 200
CAPTION_BAND_TOP = 800
BRIGHT = 240


def _deps(tmp_path):
    settings = Settings(
        workspace_dir=tmp_path / "workspace",
        provider_chains={
            "tts": ["mock"],
            "stt": ["mock"],
            "stock": ["mock"],
            "image": ["mock"],
        },
    )
    return StageDeps(
        settings=settings,
        store=ProjectStore(settings.workspace_dir),
        stage_cache=StageCache(tmp_path / "stages.json"),
        response_cache=ResponseCache(tmp_path / "responses"),
        quota=QuotaTracker(tmp_path / "quota.json"),
    )


def _project(deps, scenes=SCENES):
    project = deps.store.create("how ssds work", "tech_explainer", target_minutes=1.0)
    project.scenes = [
        Scene(
            id=sid,
            narration=narration,
            visual=SceneVisual(query=f"{sid} b-roll", kind=kind),
        )
        for sid, narration, kind in scenes
    ]
    deps.store.save(project)
    return project


def _assemble(deps, project):
    run_voice(project, deps)
    run_align(project, deps)
    run_visuals(project, deps)
    run_captions(project, deps)
    return run_assemble(project, deps)


def _expected_duration(scenes=SCENES):
    return sum(
        len(narration.split()) * SECONDS_PER_WORD + SCENE_GAP_S for _, narration, _ in scenes
    )


def _mtimes(root, project):
    return {
        scene.id: (root / segment_relpath(scene.id, Aspect.WIDE)).stat().st_mtime_ns
        for scene in project.scenes
    }


def _bright_fraction(path, at_s):
    """Fraction of near-white pixels in the caption band of one decoded frame.

    Read from the picture itself: the photo fixture is a flat slate grey, so any
    near-white pixel down there is burned-in caption text and nothing else.
    """
    crop = f"crop={WIDE_SPEC.width}:{CAPTION_BAND_HEIGHT}:0:{CAPTION_BAND_TOP}"
    raw = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-nostdin",
            "-i", str(path),
            "-ss", str(at_s),
            "-frames:v", "1",
            "-vf", f"{crop},format=gray",
            "-f", "rawvideo", "-",
        ],
        capture_output=True,
        check=True,
    ).stdout
    assert raw, f"no frame decoded from {path} at {at_s}s"
    return sum(1 for value in raw if value >= BRIGHT) / len(raw)


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    """One full assemble + render, shared by the read-only assertions below."""
    tmp_path = tmp_path_factory.mktemp("render_wide")
    deps = _deps(tmp_path)
    project = _project(deps)
    _assemble(deps, project)
    result = run_render(project, deps)
    return deps, project, result


@pytest.fixture
def assembled(tmp_path):
    """A fresh project taken as far as assembly, for the tests that mutate it."""
    deps = _deps(tmp_path)
    project = _project(deps)
    _assemble(deps, project)
    return deps, project


# --------------------------------------------------------------- the final file


def test_render_produces_a_playable_wide_video(rendered):
    deps, project, result = rendered
    path = deps.store.path_for(project.id) / output_relpath(Aspect.WIDE)

    assert result.changed is True
    assert path.is_file()
    assert path.stat().st_size > 0

    info = probe_json(path)
    streams = {stream["codec_type"]: stream for stream in info["streams"]}
    assert streams["video"]["codec_name"] == "h264"
    assert (streams["video"]["width"], streams["video"]["height"]) == WIDE_SPEC.size
    assert streams["video"]["r_frame_rate"] == f"{WIDE_SPEC.fps}/1"
    assert streams["audio"]["codec_name"] == "aac"
    assert probe_duration(path) == pytest.approx(_expected_duration(), abs=DURATION_TOLERANCE_S)


def test_the_project_records_where_the_video_landed(rendered):
    deps, project, _ = rendered
    spec = project.outputs[Aspect.WIDE]

    assert spec.video_path == output_relpath(Aspect.WIDE)
    assert (spec.width, spec.height) == WIDE_SPEC.size
    assert spec.scene_ids == [sid for sid, _, _ in SCENES]
    # And it survived to disk, not just in memory.
    assert deps.store.load(project.id).outputs[Aspect.WIDE].video_path == spec.video_path


def test_captions_are_burned_into_the_picture(rendered):
    """Metadata cannot see subtitles; pixels can."""
    deps, project, _ = rendered
    root = deps.store.path_for(project.id)
    at_s = 0.6  # inside scene one's narration, so a caption is on screen

    before = _bright_fraction(root / video_relpath(Aspect.WIDE), at_s)
    after = _bright_fraction(root / output_relpath(Aspect.WIDE), at_s)

    assert before == pytest.approx(0.0, abs=1e-4)  # the fixture is flat grey
    assert after > 0.005  # white text on a lower third


def test_the_subtitles_filter_uses_a_relative_path(rendered):
    """An absolute `.ass` path with a colon in it breaks the filter-graph parser (M0)."""
    deps, project, _ = rendered
    root = deps.store.path_for(project.id)

    graph = subtitles_filter(root, Aspect.WIDE)

    assert graph.startswith(f"subtitles={caption_relpath(Aspect.WIDE)}")
    assert str(root) not in graph
    # No fonts are bundled until M3, so nothing may point at a directory that is absent.
    assert "fontsdir" not in graph


# ------------------------------------------------------------- the intermediates


def test_the_concat_list_names_every_segment(rendered):
    deps, project, _ = rendered
    root = deps.store.path_for(project.id)

    lines = (root / concat_relpath(Aspect.WIDE)).read_text().split()

    assert lines == [item for sid, _, _ in SCENES for item in ("file", f"'{sid}_wide.mp4'")]
    assert (root / narration_relpath(Aspect.WIDE)).is_file()
    for sid, _, _ in SCENES:
        assert (root / segment_relpath(sid, Aspect.WIDE)).is_file()


def test_the_timeline_matches_the_caption_offsets(rendered):
    deps, project, _ = rendered
    root = deps.store.path_for(project.id)

    timeline = json.loads((root / timeline_relpath(Aspect.WIDE)).read_text())

    expected = scene_timeline(project, gap_s=SCENE_GAP_S)
    assert [scene["id"] for scene in timeline["scenes"]] == [seg.scene_id for seg in expected]
    assert timeline["scenes"][1]["start_s"] == pytest.approx(expected[1].start_s)
    assert timeline["gap_s"] == SCENE_GAP_S
    assert timeline["total_duration_s"] == pytest.approx(_expected_duration())


def test_each_segment_is_exactly_its_narration_plus_the_gap(rendered):
    deps, project, _ = rendered
    root = deps.store.path_for(project.id)

    for segment in scene_timeline(project, gap_s=SCENE_GAP_S):
        path = root / segment_relpath(segment.scene_id, Aspect.WIDE)
        assert probe_duration(path) == pytest.approx(segment.duration_s, abs=1 / WIDE_SPEC.fps)


def test_the_narration_bed_spans_the_whole_timeline(rendered):
    deps, project, _ = rendered
    root = deps.store.path_for(project.id)

    duration = probe_duration(root / narration_relpath(Aspect.WIDE))

    assert duration == pytest.approx(_expected_duration(), abs=0.05)


# -------------------------------------------------------------------- the cache


def test_a_clean_rerun_re_encodes_nothing(rendered):
    deps, project, _ = rendered
    root = deps.store.path_for(project.id)
    before = _mtimes(root, project)
    final = (root / output_relpath(Aspect.WIDE)).stat().st_mtime_ns

    assemble = run_assemble(project, deps)
    render = run_render(project, deps)

    assert assemble.changed is False
    assert assemble.skipped_units == len(SCENES) + 1  # every segment, plus the join
    assert render.changed is False
    assert render.skipped_units == 1
    assert _mtimes(root, project) == before
    assert (root / output_relpath(Aspect.WIDE)).stat().st_mtime_ns == final


def test_editing_one_scene_re_encodes_only_that_scene(assembled):
    deps, project = assembled
    root = deps.store.path_for(project.id)
    before = _mtimes(root, project)

    project.scene_by_id("s02").narration = "Electrons sit behind an insulator"
    result = _assemble(deps, project)

    after = _mtimes(root, project)
    assert result.changed is True
    assert after["s01"] == before["s01"]  # untouched scenes are never re-encoded
    assert after["s02"] != before["s02"]


def test_a_deleted_segment_is_re_encoded_even_though_the_hash_is_fresh(assembled):
    deps, project = assembled
    root = deps.store.path_for(project.id)
    (root / segment_relpath("s01", Aspect.WIDE)).unlink()

    result = run_assemble(project, deps)

    assert result.changed is True
    assert (root / segment_relpath("s01", Aspect.WIDE)).is_file()


def test_a_clip_shorter_than_its_segment_is_looped_to_fill_it(assembled):
    """The loop path, encoded for real: a 1 s clip under a longer scene."""
    deps, project = assembled
    root = deps.store.path_for(project.id)
    scene = project.scene_by_id("s02")
    asset = root / scene.visual.chosen.local_path
    short = asset.with_name("short.mp4")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(asset), "-t", "1", str(short)],
        check=True,
    )
    short.replace(asset)
    scene.visual.chosen.duration_s = 1.0
    scene.duration_s = 2.0

    run_assemble(project, deps)

    path = root / segment_relpath("s02", Aspect.WIDE)
    assert probe_duration(path) == pytest.approx(2.0 + SCENE_GAP_S, abs=1 / WIDE_SPEC.fps)
