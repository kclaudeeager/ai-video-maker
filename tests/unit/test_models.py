from datetime import UTC, datetime

import pytest

from videomaker.models import (
    Aspect,
    AssetRef,
    Motion,
    Project,
    Scene,
    SceneVisual,
    VisualKind,
    WordTiming,
)


def _project(**kw) -> Project:
    defaults = {
        "id": "how-ssds-work",
        "topic": "how ssds work",
        "template": "tech_explainer",
        "created_at": datetime(2026, 8, 30, tzinfo=UTC),
        "scenes": [
            Scene(id="s01", narration="First.", visual=SceneVisual(query="ssd")),
            Scene(id="s02", narration="Second.", visual=SceneVisual(query="nand")),
        ],
    }
    return Project(**{**defaults, **kw})


def test_defaults_are_sane():
    p = _project()
    assert p.language == "en"
    assert p.voice == "af_heart"
    assert p.scenes[0].in_short is True
    assert p.scenes[0].visual.kind is VisualKind.AUTO
    assert p.scenes[0].visual.motion is Motion.PAN
    assert p.scenes[0].visual.crop_focus_x == 0.5
    assert p.approvals.script is None


def test_round_trips_through_json():
    p = _project()
    p.scenes[0].words = [WordTiming(word="First", start_s=0.0, end_s=0.4)]
    p.scenes[0].visual.chosen = AssetRef(
        provider="pexels", source_id="123", source_url="https://x/1",
        local_path="scenes/s01/asset.mp4", width=1920, height=1080,
        duration_s=6.0, attribution="A Photographer", license="Pexels",
    )
    restored = Project.model_validate_json(p.model_dump_json())
    assert restored == p


def test_scene_lookup_and_next_id():
    p = _project()
    assert p.scene_by_id("s02").narration == "Second."
    assert p.next_scene_id() == "s03"
    with pytest.raises(KeyError):
        p.scene_by_id("s99")


def test_outputs_keyed_by_aspect():
    p = _project()
    p.outputs[Aspect.WIDE] = p.outputs.get(Aspect.WIDE) or _wide(p)
    assert p.outputs[Aspect.WIDE].width == 1920
    # Aspect must survive a JSON round trip as a dict key.
    restored = Project.model_validate_json(p.model_dump_json())
    assert restored.outputs[Aspect.WIDE].scene_ids == ["s01", "s02"]


def _wide(p):
    from videomaker.models import OutputSpec

    return OutputSpec(
        aspect=Aspect.WIDE, width=1920, height=1080,
        scene_ids=[s.id for s in p.scenes],
    )


def test_crop_focus_is_bounded():
    with pytest.raises(ValueError):
        SceneVisual(query="x", crop_focus_x=1.5)
