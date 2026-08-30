from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from videomaker.models import VisualKind

TEMPLATE_SUFFIX = ".yaml"
# One scene is roughly one narrated sentence; used only to turn a target
# duration into a scene count, which is then clamped by the template.
AVG_WORDS_PER_SCENE = 30
DEFAULT_TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates"


class Template(BaseModel):
    """A niche template: the editorial recipe for one kind of video.

    Templates are plain YAML in `templates/` — see `templates/_schema.md`.
    Adding one requires no Python.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    display_name: str
    system_prompt: str
    structure: list[str] = Field(min_length=1)  # named beats, in order
    words_per_minute: int = Field(150, gt=0)
    scene_count: tuple[int, int]
    visual_kind_order: list[VisualKind] = Field(min_length=1)
    caption_style: str = "default"
    music_mood: str = "calm"

    @field_validator("scene_count")
    @classmethod
    def _check_bounds(cls, value: tuple[int, int]) -> tuple[int, int]:
        low, high = value
        if low < 1 or high < low:
            raise ValueError(f"scene_count must be [min, max] with 1 <= min <= max, got {value}")
        return value

    def target_scene_count(self, minutes: float) -> int:
        """Scenes for a `minutes`-long video, clamped into this template's bounds."""
        low, high = self.scene_count
        wanted = round(minutes * self.words_per_minute / AVG_WORDS_PER_SCENE)
        return max(low, min(high, wanted))


def _resolve_dir(templates_dir: Path | None) -> Path:
    return DEFAULT_TEMPLATES_DIR if templates_dir is None else Path(templates_dir)


def list_templates(templates_dir: Path | None = None) -> list[str]:
    """Names of every template in `templates_dir`, sorted. Files starting with `_` are docs."""
    directory = _resolve_dir(templates_dir)
    if not directory.is_dir():
        return []
    return sorted(
        path.stem for path in directory.glob(f"*{TEMPLATE_SUFFIX}") if not path.name.startswith("_")
    )


def load_template(name: str, templates_dir: Path | None = None) -> Template:
    """Load and validate one template by name. Raises `ValueError` if it is missing or invalid."""
    directory = _resolve_dir(templates_dir)
    path = directory / f"{name}{TEMPLATE_SUFFIX}"
    if not path.is_file():
        available = ", ".join(list_templates(directory)) or "none"
        raise ValueError(f"unknown template {name!r} (available: {available})")
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: not valid YAML: {exc}") from exc
    try:
        # A non-mapping (or empty) file fails here as a ValidationError, which is a ValueError.
        template = Template.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"{path}: invalid template: {exc}") from exc
    if template.name != path.stem:
        raise ValueError(f"{path}: name {template.name!r} does not match filename {path.stem!r}")
    return template
