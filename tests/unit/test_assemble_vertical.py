"""Vertical assembly: every scene is re-cropped from its own source asset.

The milestone's binding constraint is that vertical is **never** a crop of the
finished wide render — a 9:16 frame carved out of the wide output would carry
wide-styled burned-in captions with it. So the vertical graph starts by cutting a
9:16 window out of the *source*, at `crop_focus_x`, before anything is scaled.

That reframe is also what finally makes `crop_focus_x` do something. Until now it
only nudged the pan across a window that was always the full width of the source.

Two properties are load-bearing enough to be asserted directly rather than inferred:

* **The reframe happens in source space, and the motion still scales before it crops.**
  `test_vertical_motion_still_scales_before_it_crops` splits the graph at the reframe
  and re-applies the wide suite's ordering rule to what follows. Cropping the *output*
  window before scaling throws away the pixels the scale needs; cropping the *source*
  window first throws away only pixels no vertical frame could ever show.
* **Segments are cached per aspect.** Re-cropping scene 3 for vertical must not
  invalidate scene 3's wide segment, or every Short would cost a full wide re-encode.
  Proven by counting `run_ffmpeg` calls and witnessing mtimes, the way M1 did.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from videomaker.cache import ResponseCache, StageCache
from videomaker.config import Settings
from videomaker.media.ffmpeg import probe_dimensions
from videomaker.models import (
    Aspect,
    AssetRef,
    Motion,
    Project,
    Scene,
    SceneVisual,
)
from videomaker.pipeline import assemble as assemble_module
from videomaker.pipeline.assemble import (
    PRESCALE_PAN,
    PRESCALE_ZOOM,
    VERTICAL_SPEC,
    WIDE_SPEC,
    build_scene_filter,
    reframe_filter,
    run_assemble,
    scene_hash,
    segment_relpath,
)
from videomaker.pipeline.base import SCENE_GAP_S, StageDeps
from videomaker.project import ProjectStore
from videomaker.providers.ratelimit import QuotaTracker

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _asset(duration_s: float | None) -> AssetRef:
    return AssetRef(
        provider="mock",
        source_id="mock-1",
        source_url="https://mock.invalid/1",
        local_path="scenes/s01/asset" + (".mp4" if duration_s else ".jpg"),
        width=1920,
        height=1080,
        duration_s=duration_s,
    )


def _scene(
    *,
    duration: float,
    asset_duration: float | None = None,
    motion: Motion = Motion.PAN,
    sid: str = "s01",
    focus: float = 0.5,
) -> Scene:
    return Scene(
        id=sid,
        narration=f"Narration for {sid}.",
        visual=SceneVisual(
            query=f"{sid} b-roll",
            chosen=_asset(asset_duration),
            motion=motion,
            crop_focus_x=focus,
        ),
        audio_path=f"scenes/{sid}/narration.wav",
        duration_s=duration,
    )


def _still(duration: float = 4.0, **kwargs) -> Scene:
    return _scene(duration=duration, asset_duration=None, **kwargs)


def _video(duration: float = 4.0, asset_duration: float = 8.0, **kwargs) -> Scene:
    return _scene(duration=duration, asset_duration=asset_duration, **kwargs)


def _vertical(scene: Scene) -> str:
    return build_scene_filter(scene, VERTICAL_SPEC, gap_s=SCENE_GAP_S)


# ------------------------------------------------------------ the reframe crop


def test_vertical_cuts_a_nine_by_sixteen_window_out_of_the_source():
    """Spec 4.5's expression, verbatim: the source's own pixels, not the wide render."""
    graph = _vertical(_still())

    assert "crop='min(iw,ih*9/16)':ih:'(iw-ow)*0.500':0" in graph


def test_the_reframe_is_the_first_thing_a_still_does():
    graph = _vertical(_still())

    assert graph.index("crop='min(iw") < graph.index("scale=")


def test_crop_focus_x_moves_the_reframe_window():
    """0.0 is the left edge, 1.0 the right — the field's whole reason to exist."""
    left = _vertical(_still(focus=0.0))
    centre = _vertical(_still(focus=0.5))
    right = _vertical(_still(focus=1.0))

    assert "'(iw-ow)*0.000':0" in left
    assert "'(iw-ow)*0.500':0" in centre
    assert "'(iw-ow)*1.000':0" in right
    assert left != right


def test_an_out_of_range_focus_is_clamped_into_the_frame():
    scene = _still()
    scene.visual.crop_focus_x = 4.0  # past the bound the model itself enforces

    assert "'(iw-ow)*1.000':0" in _vertical(scene)


def test_a_video_asset_is_reframed_after_it_is_trimmed():
    """The trim picks the seconds; the reframe picks the pixels."""
    graph = _vertical(_video())

    assert graph.index("trim=start=") < graph.index("crop='min(iw")


def test_wide_is_never_reframed():
    """M1's wide graphs stay byte-identical; `_cover_scale` already covers 16:9."""
    graph = build_scene_filter(_still(), WIDE_SPEC, gap_s=SCENE_GAP_S)

    assert "min(iw" not in graph
    assert reframe_filter(WIDE_SPEC, 0.5) == []


# ------------------------------------------------------- the rest of the graph


def test_vertical_motion_still_scales_before_it_crops():
    """The wide suite's rule, re-applied to everything after the source reframe."""
    graph = _vertical(_still())
    _, after = graph.split(reframe_filter(VERTICAL_SPEC, 0.5)[0], 1)

    assert "scale=" in after
    assert after.index("scale=") < after.index("crop=")


def test_vertical_pan_prescales_by_the_documented_factor():
    graph = _vertical(_still(motion=Motion.PAN))

    width = round(VERTICAL_SPEC.width * PRESCALE_PAN)
    height = round(VERTICAL_SPEC.height * PRESCALE_PAN)
    assert f"scale={width}:{height}" in graph
    assert f"crop={VERTICAL_SPEC.width}:{VERTICAL_SPEC.height}" in graph


def test_vertical_zoom_prescales_two_times_before_zoompan():
    graph = _vertical(_still(motion=Motion.ZOOM))

    width = round(VERTICAL_SPEC.width * PRESCALE_ZOOM)
    height = round(VERTICAL_SPEC.height * PRESCALE_ZOOM)
    assert f"scale={width}:{height}" in graph
    assert f"s={VERTICAL_SPEC.width}x{VERTICAL_SPEC.height}" in graph


def test_a_vertical_graph_is_not_a_wide_graph():
    scene = _still()

    assert _vertical(scene) != build_scene_filter(scene, WIDE_SPEC, gap_s=SCENE_GAP_S)


def test_the_two_aspects_fingerprint_a_scene_differently():
    """If they hashed alike, one aspect's segment would satisfy the other's cache."""
    scene = _still()
    wide = build_scene_filter(scene, WIDE_SPEC, gap_s=SCENE_GAP_S)

    assert scene_hash(scene, WIDE_SPEC, wide, "digest") != scene_hash(
        scene, VERTICAL_SPEC, _vertical(scene), "digest"
    )


# --------------------------------------------------------------- a real encode


def test_a_real_encode_of_a_wide_clip_is_1080x1920(tmp_path):
    """The only test here that runs FFmpeg: proof the quoted expression parses.

    `min(iw,ih*9/16)` contains a comma, which the filter-graph parser reads as the end
    of the filter unless the argument is quoted. No string assertion can catch that.
    """
    scene = _video(duration=0.5, asset_duration=8.0)
    scene.visual.chosen.local_path = "asset.mp4"
    (tmp_path / "asset.mp4").write_bytes((FIXTURES / "sample_clip.mp4").read_bytes())
    graph = build_scene_filter(scene, VERTICAL_SPEC, gap_s=0.0)

    assemble_module._encode_segment(tmp_path, scene, VERTICAL_SPEC, graph, "out.mp4")

    assert probe_dimensions(tmp_path / "out.mp4") == (1080, 1920)


# ------------------------------------------------------- per-aspect cache scope


def _deps(tmp_path: Path) -> StageDeps:
    settings = Settings(workspace_dir=tmp_path / "workspace")
    return StageDeps(
        settings=settings,
        store=ProjectStore(settings.workspace_dir),
        stage_cache=StageCache(tmp_path / "stages.json"),
        response_cache=ResponseCache(tmp_path / "responses"),
        quota=QuotaTracker(tmp_path / "quota.json"),
    )


def _project(deps: StageDeps) -> Project:
    project = deps.store.create("vertical cache", "tech_explainer", target_minutes=1.0)
    root = deps.store.path_for(project.id)
    scenes = []
    for index in range(1, 4):
        sid = f"s{index:02d}"
        asset, audio = f"scenes/{sid}/asset.jpg", f"scenes/{sid}/narration.wav"
        for relpath in (asset, audio):
            path = root / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"{relpath} bytes".encode())
        scene = _scene(duration=2.0, sid=sid)
        scene.visual.chosen.local_path = asset
        scene.audio_path = audio
        scenes.append(scene)
    project.scenes = scenes
    project.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    deps.store.save(project)
    return project


@pytest.fixture
def recorder(monkeypatch) -> list[list[str]]:
    """Stand in for FFmpeg: record the call, then create the file it promised.

    Creating the output is what lets the test witness mtimes — a segment that was not
    re-encoded still carries the mtime of the run that first wrote it.
    """
    calls: list[list[str]] = []

    def fake_run_ffmpeg(args: list[str], *, cwd: Path, on_progress=None) -> None:
        calls.append(args)
        out = cwd / args[-1]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"encoded %d" % len(calls))

    monkeypatch.setattr(assemble_module, "run_ffmpeg", fake_run_ffmpeg)
    return calls


def _segment_calls(calls: list[list[str]], project: Project, aspect: Aspect) -> list[list[str]]:
    """Just the per-scene segment encodes — `video_wide.mp4` shares their suffix."""
    wanted = {segment_relpath(scene.id, aspect) for scene in project.scenes}
    return [args for args in calls if args[-1] in wanted]


def _mtimes(root: Path, project: Project, aspect: Aspect) -> dict[str, int]:
    return {
        scene.id: (root / segment_relpath(scene.id, aspect)).stat().st_mtime_ns
        for scene in project.scenes
    }


def test_building_vertical_leaves_every_wide_segment_untouched(monkeypatch, tmp_path, recorder):
    """The guard: a Short costs vertical encodes only, never a wide re-encode."""
    deps = _deps(tmp_path)
    project = _project(deps)
    root = deps.store.path_for(project.id)
    run_assemble(project, deps)  # wide alone, as the stage ships today
    before = _mtimes(root, project, Aspect.WIDE)
    recorder.clear()

    monkeypatch.setattr(assemble_module, "ASSEMBLE_ASPECTS", (Aspect.WIDE, Aspect.VERTICAL))
    run_assemble(project, deps)

    assert len(_segment_calls(recorder, project, Aspect.VERTICAL)) == 3
    assert _segment_calls(recorder, project, Aspect.WIDE) == []
    assert _mtimes(root, project, Aspect.WIDE) == before


def test_a_vertical_build_leaves_the_wide_cache_entries_fresh(monkeypatch, tmp_path, recorder):
    """The other half: vertical must not overwrite wide's fingerprints either."""
    deps = _deps(tmp_path)
    project = _project(deps)
    root = deps.store.path_for(project.id)
    monkeypatch.setattr(assemble_module, "ASSEMBLE_ASPECTS", (Aspect.WIDE, Aspect.VERTICAL))
    run_assemble(project, deps)
    before = _mtimes(root, project, Aspect.WIDE)
    recorder.clear()

    monkeypatch.setattr(assemble_module, "ASSEMBLE_ASPECTS", (Aspect.WIDE,))
    result = run_assemble(project, deps)

    assert recorder == []  # nothing at all is re-encoded
    assert result.changed is False
    assert _mtimes(root, project, Aspect.WIDE) == before


def test_the_two_aspects_write_to_different_files():
    assert segment_relpath("s03", Aspect.WIDE) != segment_relpath("s03", Aspect.VERTICAL)
