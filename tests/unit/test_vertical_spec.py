"""The vertical frame, the per-aspect plumbing and the `in_short` subset.

The test that matters most here is `test_wide_unit_hashes_are_unchanged_by_vertical`.
Every existing project — including the owner's finished one — keeps its rendered video
only because the *wide* fingerprints in `cache/stages.json` still match what
`STAGE_UNITS` computes. Adding a second aspect must therefore be additive: the wide
hashes are pinned here as literals captured from the pre-vertical code, so recomputing
them with the new code can never make the assertion vacuous.

The second thing under test is the `required` derivation. While no stage executed
vertical, its units were listed and **not** required, so a finished wide project still
derived as `rendered`. Task 6 put vertical into all three stage tuples, and the same
derivation now makes those units required — so a project that has only ever rendered
wide honestly stops being `rendered` until it builds its Short, and becomes `rendered`
again the moment it has one. Both halves are asserted below.
"""

from datetime import UTC, datetime

import pytest

from videomaker.cache import STAGE_ORDER, StageCache
from videomaker.models import (
    Approvals,
    Aspect,
    AssetRef,
    Motion,
    OutputSpec,
    Project,
    Scene,
    SceneVisual,
    Status,
    WordTiming,
)
from videomaker.pipeline.assemble import (
    ASSEMBLE_ASPECTS,
    MAX_SHORT_S,
    SPECS,
    VERTICAL_SPEC,
    WIDE_SPEC,
    short_duration_s,
    short_fits,
    timeline_scene_ids,
)
from videomaker.pipeline.captions import CAPTION_ASPECTS
from videomaker.pipeline.render import RENDER_ASPECTS
from videomaker.runner import STAGE_UNITS, derive_status, stamp_stage

#: The wide fingerprints of `_project()` as computed by the pre-vertical (M1/M2) code.
#: Captured by running `STAGE_UNITS[stage](_project())` against that code and pinning
#: the result; never regenerate these from the current tree — that is the one edit
#: that would turn this test into a tautology.
M1_WIDE_FINGERPRINTS: dict[str, str] = {
    "captions": "66c6be2d14ce8fbe",
    "assemble": "cb9c888d614ac438",
    "render": "6bc8e2b8430ab002",
}

PER_ASPECT_STAGES: tuple[str, ...] = ("captions", "assemble", "render")


def _asset(sid: str, duration_s: float | None) -> AssetRef:
    return AssetRef(
        provider="mock",
        source_id=f"mock-{sid}",
        source_url=f"https://mock.invalid/{sid}",
        local_path=f"scenes/{sid}/asset" + (".mp4" if duration_s else ".jpg"),
        width=1920,
        height=1080,
        duration_s=duration_s,
    )


def _words(sid: str, count: int) -> list[WordTiming]:
    return [
        WordTiming(word=f"{sid}-w{index}", start_s=index * 0.5, end_s=index * 0.5 + 0.5)
        for index in range(count)
    ]


def _scene(
    sid: str,
    *,
    duration: float | None,
    in_short: bool = True,
    motion: Motion = Motion.PAN,
    focus: float = 0.5,
) -> Scene:
    return Scene(
        id=sid,
        narration=f"Narration for {sid}.",
        visual=SceneVisual(
            query=f"query {sid}",
            chosen=_asset(sid, 12.0),
            motion=motion,
            crop_focus_x=focus,
            trim_start_s=0.0,
        ),
        audio_path=f"scenes/{sid}/narration.wav" if duration is not None else None,
        duration_s=duration,
        words=_words(sid, 4) if duration is not None else [],
        in_short=in_short,
    )


def _project() -> Project:
    """The representative project the pinned hashes were captured from. Frozen.

    Four voiced scenes, one of them (`s03`) excluded from the Short, so wide and
    vertical disagree on both membership and duration.
    """
    return Project(
        id="pinned",
        topic="how ssds work",
        template="tech_explainer",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        scenes=[
            _scene("s01", duration=8.0),
            _scene("s02", duration=6.5, motion=Motion.ZOOM, focus=0.25),
            _scene("s03", duration=9.25, in_short=False),
            _scene("s04", duration=7.0, focus=0.75),
        ],
        outputs={
            Aspect.WIDE: OutputSpec(
                aspect=Aspect.WIDE,
                width=1920,
                height=1080,
                scene_ids=["s01", "s02", "s03", "s04"],
                video_path="output/final_wide.mp4",
            )
        },
    )


def _unit(stage: str, project: Project, aspect: Aspect):
    for unit in STAGE_UNITS[stage](project):
        if unit.unit == aspect.value:
            return unit
    raise AssertionError(f"no {aspect.value} unit for stage {stage}")


# --------------------------------------------------------------- the trap: wide hashes


@pytest.mark.parametrize("stage", PER_ASPECT_STAGES)
def test_wide_unit_hashes_are_unchanged_by_vertical(stage):
    """Byte-identical to M1, or every existing project re-renders from scratch."""
    assert _unit(stage, _project(), Aspect.WIDE).fingerprint == M1_WIDE_FINGERPRINTS[stage]


@pytest.mark.parametrize("stage", PER_ASPECT_STAGES)
def test_the_in_short_flags_do_not_move_a_wide_hash(stage):
    """`in_short` is a vertical-only input; touching it must not restage the wide cut."""
    before = _unit(stage, _project(), Aspect.WIDE).fingerprint
    project = _project()
    for scene in project.scenes:
        scene.in_short = not scene.in_short
    assert _unit(stage, project, Aspect.WIDE).fingerprint == before


@pytest.mark.parametrize("stage", PER_ASPECT_STAGES)
def test_the_in_short_flags_do_move_the_vertical_hash(stage):
    """...and the vertical cut must notice, or a re-cut Short would never re-render."""
    before = _unit(stage, _project(), Aspect.VERTICAL).fingerprint
    project = _project()
    project.scene_by_id("s03").in_short = True
    assert _unit(stage, project, Aspect.VERTICAL).fingerprint != before


# ------------------------------------------------------------------------ the spec


def test_the_vertical_spec_is_a_1080x1920_30fps_frame():
    assert SPECS[Aspect.VERTICAL] is VERTICAL_SPEC
    assert (VERTICAL_SPEC.width, VERTICAL_SPEC.height, VERTICAL_SPEC.fps) == (1080, 1920, 30)


def test_the_wide_spec_is_untouched():
    assert (WIDE_SPEC.width, WIDE_SPEC.height, WIDE_SPEC.fps) == (1920, 1080, 30)


# ------------------------------------------------------------------- the in_short cut


def test_the_wide_timeline_is_every_assemblable_scene():
    assert timeline_scene_ids(_project(), Aspect.WIDE) == ["s01", "s02", "s03", "s04"]


def test_the_vertical_timeline_is_the_in_short_subset_in_the_same_order():
    assert timeline_scene_ids(_project(), Aspect.VERTICAL) == ["s01", "s02", "s04"]


def test_an_unvoiced_scene_is_in_neither_timeline():
    project = _project()
    project.scenes.append(_scene("s05", duration=None))
    assert timeline_scene_ids(project, Aspect.WIDE) == ["s01", "s02", "s03", "s04"]
    assert timeline_scene_ids(project, Aspect.VERTICAL) == ["s01", "s02", "s04"]


def test_the_short_duration_counts_only_in_short_scenes_and_their_gaps():
    # (8.0 + 6.5 + 7.0) narration + three 0.5 s gaps.
    assert short_duration_s(_project()) == pytest.approx(23.0)


def test_a_short_over_three_minutes_does_not_fit():
    assert MAX_SHORT_S == 180.0
    assert short_fits(_project())
    project = _project()
    project.scene_by_id("s01").duration_s = MAX_SHORT_S
    assert not short_fits(project)


# ------------------------------------------------------------------ per-aspect units


@pytest.mark.parametrize("stage", PER_ASPECT_STAGES)
def test_every_per_aspect_stage_lists_a_vertical_unit(stage):
    units = {unit.unit for unit in STAGE_UNITS[stage](_project())}
    assert units == {Aspect.WIDE.value, Aspect.VERTICAL.value}


@pytest.mark.parametrize("stage", PER_ASPECT_STAGES)
def test_a_vertical_unit_is_required_exactly_when_its_stage_executes_it(stage):
    """`required` is derived, never declared — which is how Task 6 unblocked it."""
    executed = {"captions": CAPTION_ASPECTS, "assemble": ASSEMBLE_ASPECTS, "render": RENDER_ASPECTS}
    project = _project()
    assert _unit(stage, project, Aspect.WIDE).required is True
    vertical = _unit(stage, project, Aspect.VERTICAL)
    assert vertical.required is (Aspect.VERTICAL in executed[stage])
    # ...and every stage executes it now, so every vertical unit blocks.
    assert vertical.required is True


def test_a_project_that_has_never_built_a_short_reports_its_vertical_work_undone():
    """`produced` reads the artefacts, so it stays False until the Short exists."""
    project = _project()  # a wide `OutputSpec` and nothing else
    assert _unit("assemble", project, Aspect.VERTICAL).produced is False
    assert _unit("render", project, Aspect.VERTICAL).produced is False


# ------------------------------------------------------------------- derived status


def _stamped(project: Project, tmp_path) -> StageCache:
    """Every approval given and every produced unit fingerprinted — a finished run."""
    now = datetime.now(UTC)
    project.approvals = Approvals(script=now, storyboard=now, preview=now)
    cache = StageCache(tmp_path / "stages.json")
    for stage in STAGE_ORDER:
        stamp_stage(project, cache, stage)
    return cache


def test_a_wide_only_project_stops_being_rendered_once_the_short_is_required(tmp_path):
    """The deliberate cost of Task 6, asserted rather than discovered.

    A project rendered before vertical shipped has no `final_vertical.mp4`, and with
    the vertical units now required it says so instead of claiming to be finished.
    It falls back to `storyboard_ready` — the last status whose stages are all
    current — and one `videomaker run` builds the Short and restores `rendered`.
    Nothing wide is re-encoded on the way: the wide fingerprints are unchanged (see
    `test_wide_unit_hashes_are_unchanged_by_vertical`).
    """
    project = _project()
    assert derive_status(project, _stamped(project, tmp_path)) is Status.STORYBOARD_READY


def test_the_same_project_derives_as_rendered_once_it_has_a_short(tmp_path):
    """...and this is the half that makes the drop temporary rather than permanent."""
    project = _project()
    project.outputs[Aspect.VERTICAL] = OutputSpec(
        aspect=Aspect.VERTICAL,
        width=VERTICAL_SPEC.width,
        height=VERTICAL_SPEC.height,
        scene_ids=["s01", "s02", "s04"],
        video_path="output/final_vertical.mp4",
    )
    assert derive_status(project, _stamped(project, tmp_path)) is Status.RENDERED
