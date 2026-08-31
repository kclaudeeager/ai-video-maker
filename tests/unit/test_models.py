from datetime import UTC, datetime
from pathlib import Path

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


# --------------------------------------------------- AssetRef.preview_url (M3 Task 2)

#: A `project.json` written before `AssetRef.preview_url` existed — a trimmed copy of
#: the owner's real finished project, kept byte-for-byte in the pre-change shape.
_PRE_PREVIEW_URL = Path(__file__).parent.parent / "fixtures" / "project_pre_preview_url.json"


def test_asset_ref_preview_url_defaults_to_empty():
    ref = AssetRef(
        provider="pexels", source_id="123", source_url="https://x/1",
        local_path="", width=1920, height=1080,
    )
    assert ref.preview_url == ""


def test_a_project_json_written_before_preview_url_still_loads():
    """The real backward-compatibility guarantee: old projects must not stop opening.

    The fixture is a real pre-change file, so this fails the moment the field is
    made required — a default that only *looks* optional would not survive it.
    """
    blob = _PRE_PREVIEW_URL.read_text()
    assert "preview_url" not in blob, "the fixture must hold the PRE-change shape"

    project = Project.model_validate_json(blob)

    assert project.id == "how-ssds-work"
    assert project.approvals.preview is not None
    assert project.outputs[Aspect.WIDE].video_path == "output/final_wide.mp4"
    refs = [
        ref
        for scene in project.scenes
        for ref in [*scene.visual.candidates, scene.visual.chosen]
        if ref is not None
    ]
    assert refs, "the fixture must actually carry assets"
    assert all(ref.preview_url == "" for ref in refs)


def test_preview_url_survives_a_json_round_trip():
    p = _project()
    p.scenes[0].visual.candidates = [
        AssetRef(
            provider="pexels", source_id="123", source_url="https://x/1",
            local_path="", width=1920, height=1080, duration_s=6.0,
            preview_url="https://images.pexels.com/videos/123/thumb.jpeg",
        )
    ]
    restored = Project.model_validate_json(p.model_dump_json())
    assert restored.scenes[0].visual.candidates[0].preview_url == (
        "https://images.pexels.com/videos/123/thumb.jpeg"
    )
