import pytest

from videomaker.models import VisualKind
from videomaker.templates import Template, list_templates, load_template  # noqa: F401


def test_tech_explainer_loads():
    t = load_template("tech_explainer")
    assert t.name == "tech_explainer"
    assert t.system_prompt.strip()
    assert len(t.structure) >= 3
    assert t.visual_kind_order[0] in set(VisualKind)


def test_unknown_template_raises_with_available_names():
    with pytest.raises(ValueError) as exc:
        load_template("no_such_template")
    assert "tech_explainer" in str(exc.value)


def test_list_templates_includes_shipped_ones():
    assert "tech_explainer" in list_templates()


@pytest.mark.parametrize("minutes,expected_range", [(0.5, (3, 12)), (2.0, (3, 12)), (10.0, (3, 12))])
def test_target_scene_count_stays_in_bounds(minutes, expected_range):
    t = load_template("tech_explainer")
    n = t.target_scene_count(minutes)
    assert expected_range[0] <= n <= expected_range[1]


def test_invalid_template_yaml_is_rejected(tmp_path):
    (tmp_path / "broken.yaml").write_text("display_name: no name field\n")
    with pytest.raises(ValueError):
        load_template("broken", templates_dir=tmp_path)
