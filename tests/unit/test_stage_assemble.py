"""The assemble stage's pure half: the per-scene filter graph and the timeline.

`build_scene_filter` is deliberately a pure string-returning function. A filter graph
that is wrong by one operation — cropping before scaling, trimming to the narration
instead of the narration plus the gap — produces a video that *plays*, so only a test
that reads the graph itself can name the fault. Everything here is a string assertion;
the real FFmpeg pass lives in `tests/integration/test_render_wide.py`.

`test_the_timeline_agrees_with_the_captions_offsets` is the one that matters most:
`captions` and `assemble` compute the same offsets from `SCENE_GAP_S` independently,
and if they ever disagree the burned subtitles drift off the narration from scene two
onwards — a defect no single-scene test can see.
"""

from datetime import UTC, datetime

import pytest

from videomaker.models import (
    AssetRef,
    Motion,
    Project,
    Scene,
    SceneVisual,
    WordTiming,
)
from videomaker.pipeline.assemble import (
    PRESCALE_PAN,
    PRESCALE_ZOOM,
    WIDE_SPEC,
    build_scene_filter,
    scene_timeline,
    segment_duration,
)
from videomaker.pipeline.base import SCENE_GAP_S
from videomaker.pipeline.captions import timeline_words


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
    trim_start_s: float = 0.0,
) -> Scene:
    return Scene(
        id=sid,
        narration=f"Narration for {sid}.",
        visual=SceneVisual(
            query=f"{sid} b-roll",
            chosen=_asset(asset_duration),
            motion=motion,
            crop_focus_x=focus,
            trim_start_s=trim_start_s,
        ),
        audio_path=f"scenes/{sid}/narration.wav",
        duration_s=duration,
    )


def _still_scene(duration: float, **kwargs) -> Scene:
    return _scene(duration=duration, asset_duration=None, **kwargs)


def _video_scene(duration: float, asset_duration: float, **kwargs) -> Scene:
    return _scene(duration=duration, asset_duration=asset_duration, **kwargs)


# ------------------------------------------------------------------ the graph


def test_still_pan_filter_prescales_before_cropping():
    graph = build_scene_filter(_still_scene(duration=4.0), WIDE_SPEC, gap_s=SCENE_GAP_S)

    assert "scale=" in graph
    assert graph.index("scale=") < graph.index("crop=")  # never crop before scaling


def test_video_scene_is_trimmed_and_looped_to_exact_duration():
    graph = build_scene_filter(
        _video_scene(duration=4.0, asset_duration=2.0), WIDE_SPEC, gap_s=SCENE_GAP_S
    )

    assert "loop" in graph or "stream_loop" in graph


def test_pan_prescales_by_the_documented_factor():
    graph = build_scene_filter(_still_scene(duration=4.0), WIDE_SPEC, gap_s=SCENE_GAP_S)

    width = round(WIDE_SPEC.width * PRESCALE_PAN)
    height = round(WIDE_SPEC.height * PRESCALE_PAN)
    assert f"scale={width}:{height}" in graph
    assert f"crop={WIDE_SPEC.width}:{WIDE_SPEC.height}" in graph


def test_zoom_prescales_two_times_before_zoompan():
    """Ken Burns without a pre-upscale visibly jitters (the M0 spike measured it)."""
    graph = build_scene_filter(
        _still_scene(duration=4.0, motion=Motion.ZOOM), WIDE_SPEC, gap_s=SCENE_GAP_S
    )

    assert PRESCALE_ZOOM == 2.0
    assert f"scale={WIDE_SPEC.width * 2}:{WIDE_SPEC.height * 2}" in graph
    assert graph.index("scale=") < graph.index("zoompan=")


def test_motion_none_holds_a_still_frame():
    graph = build_scene_filter(
        _still_scene(duration=4.0, motion=Motion.NONE), WIDE_SPEC, gap_s=SCENE_GAP_S
    )

    assert "crop=" in graph
    # No `t` in the crop offsets: a static crop, or it is not motion NONE.
    crop = next(part for part in graph.split(",") if part.startswith("crop="))
    assert "t/" not in crop


def test_a_video_is_never_panned():
    """Motion is a still-image treatment; footage already moves."""
    graph = build_scene_filter(
        _video_scene(duration=2.0, asset_duration=8.0, motion=Motion.PAN),
        WIDE_SPEC,
        gap_s=SCENE_GAP_S,
    )

    crop = next(part for part in graph.split(",") if part.startswith("crop="))
    assert "t/" not in crop


def test_crop_focus_biases_the_pan_away_from_the_edge():
    left = build_scene_filter(
        _still_scene(duration=4.0, focus=0.0), WIDE_SPEC, gap_s=SCENE_GAP_S
    )
    centred = build_scene_filter(_still_scene(duration=4.0), WIDE_SPEC, gap_s=SCENE_GAP_S)

    assert left != centred
    # A focus hard against the left edge can never travel to the right edge.
    assert "1.000" not in next(part for part in left.split(",") if part.startswith("crop="))


def test_pan_direction_alternates_between_neighbouring_scenes():
    odd = build_scene_filter(_still_scene(duration=4.0, sid="s01"), WIDE_SPEC, gap_s=SCENE_GAP_S)
    even = build_scene_filter(_still_scene(duration=4.0, sid="s02"), WIDE_SPEC, gap_s=SCENE_GAP_S)

    assert odd != even  # every shot drifting the same way looks mechanical


# ------------------------------------------------------- the gap arithmetic


def test_the_segment_lasts_the_narration_plus_the_gap():
    scene = _still_scene(duration=4.0)

    assert segment_duration(scene, gap_s=SCENE_GAP_S) == pytest.approx(4.0 + SCENE_GAP_S)
    graph = build_scene_filter(scene, WIDE_SPEC, gap_s=SCENE_GAP_S)
    assert f"trim=duration={4.0 + SCENE_GAP_S:.3f}" in graph


def test_a_longer_gap_lengthens_the_segment():
    graph = build_scene_filter(_still_scene(duration=4.0), WIDE_SPEC, gap_s=1.25)

    assert "trim=duration=5.250" in graph


def test_a_video_longer_than_the_segment_is_only_trimmed():
    graph = build_scene_filter(
        _video_scene(duration=4.0, asset_duration=30.0), WIDE_SPEC, gap_s=SCENE_GAP_S
    )

    assert "loop=" not in graph
    assert "trim=start=0.000:duration=4.500" in graph


def test_trim_start_skips_into_the_asset():
    graph = build_scene_filter(
        _video_scene(duration=2.0, asset_duration=8.0, trim_start_s=3.0),
        WIDE_SPEC,
        gap_s=SCENE_GAP_S,
    )

    assert "trim=start=3.000:duration=2.500" in graph


def test_a_scene_with_no_measured_duration_is_rejected():
    scene = _still_scene(duration=4.0)
    scene.duration_s = None

    with pytest.raises(ValueError, match="duration"):
        build_scene_filter(scene, WIDE_SPEC, gap_s=SCENE_GAP_S)


def test_a_scene_with_no_chosen_asset_is_rejected():
    scene = _still_scene(duration=4.0)
    scene.visual.chosen = None

    with pytest.raises(ValueError, match="asset"):
        build_scene_filter(scene, WIDE_SPEC, gap_s=SCENE_GAP_S)


# ---------------------------------------------------------------- the timeline


def _project(durations: dict[str, float]) -> Project:
    project = Project(id="p", topic="t", template="tech_explainer", created_at=datetime.now(UTC))
    project.scenes = [
        _scene(duration=duration, sid=sid, asset_duration=None)
        for sid, duration in durations.items()
    ]
    for scene in project.scenes:
        step = (scene.duration_s or 0.0) / 3
        scene.words = [
            WordTiming(word=f"w{i}", start_s=i * step, end_s=(i + 1) * step) for i in range(3)
        ]
    return project


def test_scene_two_starts_after_scene_one_plus_the_gap():
    project = _project({"s01": 2.0, "s02": 3.0, "s03": 1.5})

    segments = scene_timeline(project, gap_s=SCENE_GAP_S)

    assert [segment.scene_id for segment in segments] == ["s01", "s02", "s03"]
    assert segments[0].start_s == pytest.approx(0.0)
    assert segments[1].start_s == pytest.approx(2.0 + SCENE_GAP_S)
    assert segments[2].start_s == pytest.approx(2.0 + 3.0 + 2 * SCENE_GAP_S)
    assert segments[2].duration_s == pytest.approx(1.5 + SCENE_GAP_S)


def test_the_timeline_agrees_with_the_captions_offsets():
    """The desync guard: captions and assembly must place scene two identically."""
    project = _project({"s01": 2.0, "s02": 3.0, "s03": 1.5})

    segments = scene_timeline(project, gap_s=SCENE_GAP_S)
    words = timeline_words(project)

    for index, segment in enumerate(segments):
        assert words[index * 3].start_s == pytest.approx(segment.start_s)


def test_unassemblable_scenes_take_no_time_on_the_timeline():
    """A scene with no audio or no asset produces no segment, so it must not shift one."""
    project = _project({"s01": 2.0, "s02": 3.0, "s03": 1.5})
    project.scene_by_id("s02").duration_s = None
    project.scene_by_id("s02").words = []

    segments = scene_timeline(project, gap_s=SCENE_GAP_S)

    assert [segment.scene_id for segment in segments] == ["s01", "s03"]
    assert segments[1].start_s == pytest.approx(2.0 + SCENE_GAP_S)
    assert timeline_words(project)[3].start_s == pytest.approx(segments[1].start_s)
