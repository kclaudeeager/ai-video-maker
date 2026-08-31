from pathlib import Path

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from videomaker.media.audio import DEFAULT_SFX_PROFILE, SFX_PROFILES
from videomaker.models import VisualKind

TEMPLATE_SUFFIX = ".yaml"
# One scene is roughly one narrated sentence; used only to turn a target
# duration into a scene count, which is then clamped by the template.
AVG_WORDS_PER_SCENE = 30
DEFAULT_TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates"

#: What a new template should usually put in `short_beats`: the opening, the heart
#: and the landing. It is a *recommendation for template authors*, not a fallback —
#: `short_beats` defaults to empty, meaning "this template has no opinion", and a
#: template with no opinion keeps every scene in the Short exactly as before.
RECOMMENDED_SHORT_BEATS = ("hook", "mechanism", "close")

#: What `sfx_profile` may say. The levels themselves live in `media/audio.py`,
#: which is what reads them; a template names a house style, not a dB.
SFX_PROFILE_NAMES: tuple[str, ...] = tuple(SFX_PROFILES)


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
    #: The beats a vertical Short is cut from. Empty means the template has no
    #: opinion and every scene stays in the Short — the pre-M3 behaviour, kept so
    #: an existing template does not change meaning when this field appears.
    short_beats: list[str] = Field(default_factory=list)
    words_per_minute: int = Field(150, gt=0)
    scene_count: tuple[int, int]
    visual_kind_order: list[VisualKind] = Field(min_length=1)
    caption_style: str = "default"
    music_mood: str = "calm"
    #: How loud this template's sound effects sit: `subtle`, `punchy` or `none`.
    #: Effects come from the user's own `assets/sfx/`, so on a fresh clone every
    #: profile including `punchy` places nothing at all. See `docs/audio-design.md`.
    sfx_profile: str = DEFAULT_SFX_PROFILE

    @field_validator("scene_count")
    @classmethod
    def _check_bounds(cls, value: tuple[int, int]) -> tuple[int, int]:
        low, high = value
        if low < 1 or high < low:
            raise ValueError(f"scene_count must be [min, max] with 1 <= min <= max, got {value}")
        return value

    @field_validator("sfx_profile")
    @classmethod
    def _known_profile(cls, value: str) -> str:
        """A profile the mixer does not know would silently place nothing.

        Which is indistinguishable from an empty `assets/sfx/`, so a typo here would
        only ever be noticed by watching a finished video and wondering why the cuts
        went quiet. Fail on the template instead.
        """
        if value not in SFX_PROFILES:
            raise ValueError(
                f"sfx_profile {value!r} is not one of {', '.join(SFX_PROFILE_NAMES)}"
            )
        return value

    @model_validator(mode="after")
    def _short_beats_are_real_beats(self) -> "Template":
        """Every `short_beats` entry must name a beat in `structure`.

        A typo would not fail loudly anywhere else: it would simply match no scene,
        the Short would come out empty, and gate 3 would refuse to render it with a
        message about unticking scenes the author never ticked.
        """
        unknown = [beat for beat in self.short_beats if beat not in self.structure]
        if unknown:
            raise ValueError(
                f"short_beats {unknown} are not in structure {self.structure}"
            )
        return self

    def script_fingerprint(self) -> str:
        """The parts of this template the **script stage** is a function of.

        Deliberately `model_dump_json` minus `short_beats` and `sfx_profile`, and that
        exclusion is load-bearing in two directions:

        * Neither field changes anything the model writes. `short_beats` only picks
          which scenes start out ticked for the vertical cut, a per-scene flag a
          person edits afterwards; `sfx_profile` only sets how loud a whoosh sits in
          the final mix. Hashing either would make an editorial retune of the Short,
          or turning the effects down, rewrite the narration.
        * The dump is the *whole* model, so **adding the field at all** would have
          moved every existing template's fingerprint and re-derived every rendered
          project as `new` — with `run_script` then replacing its scenes, and every
          voiced take, chosen shot and approval built on them. Excluding it keeps the
          bytes identical to what M1 and M2 hashed;
          `tests/unit/test_short_selection.py` pins them as literals.

        A field added here in future must make the same call explicitly: if it does
        not change what the model writes, exclude it and extend that test.
        """
        return self.model_dump_json(exclude={"short_beats", "sfx_profile"})

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
