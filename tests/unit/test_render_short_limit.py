"""The three-minute rule standing between the `in_short` subset and a Short.

`assemble.short_fits` is a **duration** test and nothing else, so it answers `True`
for a project with nothing ticked at all — 0 s fits. `render.check_short_limit` is
the *publishing* test: it refuses an over-long cut and an empty one, and it never
truncates. Truncation is the tempting fix and the forbidden one: a Short cut off at
exactly 180.000 s ends mid-sentence, and nothing in the file says why.

The message is under test as much as the refusal, because "too long" on its own is
not actionable. The user's only lever is the `in_short` tick, so the message has to
name the scenes worth unticking and say how much time unticking them buys.
"""

from datetime import UTC, datetime

import pytest

from videomaker.models import (
    Aspect,
    AssetRef,
    Project,
    Scene,
    SceneVisual,
)
from videomaker.pipeline.assemble import (
    ASSEMBLE_ASPECTS,
    MAX_SHORT_S,
    short_duration_s,
    short_fits,
)
from videomaker.pipeline.base import SCENE_GAP_S
from videomaker.pipeline.captions import CAPTION_ASPECTS
from videomaker.pipeline.render import (
    RENDER_ASPECTS,
    ShortNotRenderable,
    check_short_limit,
    scenes_to_untick,
)


def _scene(sid: str, duration: float | None, *, in_short: bool = True) -> Scene:
    return Scene(
        id=sid,
        narration=f"Narration for {sid}.",
        visual=SceneVisual(
            query=f"query {sid}",
            chosen=AssetRef(
                provider="mock",
                source_id=f"mock-{sid}",
                source_url=f"https://mock.invalid/{sid}",
                local_path=f"scenes/{sid}/asset.jpg",
                width=1920,
                height=1080,
            ),
        ),
        audio_path=f"scenes/{sid}/narration.wav" if duration is not None else None,
        duration_s=duration,
        in_short=in_short,
    )


def _project(*scenes: Scene) -> Project:
    return Project(
        id="short-limit",
        topic="how ssds work",
        template="tech_explainer",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        scenes=list(scenes),
    )


def _fills(*, count: int, in_short: bool = True) -> list[Scene]:
    """`count` scenes that together run exactly `MAX_SHORT_S`, gaps included."""
    each = MAX_SHORT_S / count - SCENE_GAP_S
    return [_scene(f"s{index:02d}", each, in_short=in_short) for index in range(1, count + 1)]


# ------------------------------------------------------------------ the switch is on


def test_every_per_aspect_stage_now_executes_vertical():
    """Task 6 is the switch: with these three tuples grown, the Short is built."""
    for tuple_ in (CAPTION_ASPECTS, ASSEMBLE_ASPECTS, RENDER_ASPECTS):
        assert Aspect.VERTICAL in tuple_
        assert Aspect.WIDE in tuple_


# --------------------------------------------------------------------- inside the limit


def test_a_short_inside_the_limit_is_accepted():
    project = _project(_scene("s01", 8.0), _scene("s02", 6.5, in_short=False))
    assert short_duration_s(project) == pytest.approx(8.5)
    check_short_limit(project)  # does not raise


def test_a_short_of_exactly_the_limit_is_accepted():
    """The boundary is inclusive: 180.000 s *is* a three-minute Short."""
    project = _project(*_fills(count=3))
    assert short_duration_s(project) == pytest.approx(MAX_SHORT_S)
    check_short_limit(project)


def test_nothing_is_suggested_for_unticking_while_the_short_fits():
    assert scenes_to_untick(_project(_scene("s01", 8.0))) == []


def test_a_long_project_is_fine_as_long_as_its_short_is_not():
    """Only the vertical cut is capped — the wide video may run as long as it likes."""
    project = _project(
        _scene("s01", 600.0, in_short=False),
        _scene("s02", 20.0),
    )
    check_short_limit(project)


def test_an_unvoiced_scene_reserves_no_time_in_the_short():
    """`scene_timeline` skips it, so the guard must not count it either."""
    project = _project(_scene("s01", 20.0), _scene("s02", None))
    check_short_limit(project)
    assert scenes_to_untick(project) == []


# ---------------------------------------------------------------------- over the limit


def test_an_over_long_short_is_refused():
    project = _project(*_fills(count=3), _scene("s99", 30.0))

    with pytest.raises(ShortNotRenderable) as excinfo:
        check_short_limit(project)

    message = str(excinfo.value)
    assert "180" in message
    assert f"{short_duration_s(project):.1f}" in message
    assert "final_vertical.mp4" in message


def test_the_refusal_names_the_scenes_to_untick_longest_first():
    project = _project(
        _scene("s01", 10.0),
        _scene("s02", 120.0),
        _scene("s03", 90.0),
        _scene("s04", 5.0),
    )

    with pytest.raises(ShortNotRenderable) as excinfo:
        check_short_limit(project)

    message = str(excinfo.value)
    # 227 s total; dropping the longest (120.5 s) already gets under 180.
    assert "s02" in message
    assert "120.5" in message  # its whole slot, gap included
    assert "s04" not in message  # the short ones are never the advice


def test_unticking_the_named_scenes_really_does_bring_the_short_under_the_limit():
    """The advice is checked by taking it, not by reading it."""
    project = _project(
        _scene("s01", 100.0),
        _scene("s02", 90.0),
        _scene("s03", 80.0),
        _scene("s04", 70.0),
    )
    assert not short_fits(project)

    for segment in scenes_to_untick(project):
        project.scene_by_id(segment.scene_id).in_short = False

    assert short_fits(project)
    check_short_limit(project)


def test_the_advice_is_the_fewest_scenes_that_will_do():
    """Unticking one 100 s scene is enough; the guard must not ask for two."""
    project = _project(_scene("s01", 100.0), _scene("s02", 90.0), _scene("s03", 80.0))
    assert [segment.scene_id for segment in scenes_to_untick(project)] == ["s01"]


def test_the_guard_never_truncates_the_cut():
    """The whole point: an over-long Short is an error, never a shortened timeline."""
    project = _project(*_fills(count=2), _scene("s99", 60.0))
    before = [scene.in_short for scene in project.scenes]

    with pytest.raises(ShortNotRenderable):
        check_short_limit(project)

    # Nothing was silently re-cut on the user's behalf, either.
    assert [scene.in_short for scene in project.scenes] == before
    assert short_duration_s(project) > MAX_SHORT_S


# --------------------------------------------------------------------- the empty Short


def test_an_empty_short_is_refused_even_though_it_vacuously_fits():
    project = _project(_scene("s01", 8.0, in_short=False), _scene("s02", 6.5, in_short=False))
    assert short_fits(project) is True  # 0 s "fits" — the trap this guard closes

    with pytest.raises(ShortNotRenderable) as excinfo:
        check_short_limit(project)

    assert "nothing is marked for the Short" in str(excinfo.value)


def test_a_project_whose_in_short_scenes_are_all_unvoiced_is_an_empty_short():
    project = _project(_scene("s01", None), _scene("s02", 6.5, in_short=False))

    with pytest.raises(ShortNotRenderable, match="nothing is marked for the Short"):
        check_short_limit(project)
