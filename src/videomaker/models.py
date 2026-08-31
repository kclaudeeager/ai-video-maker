import re
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

#: Anything a terminal, a log line or a filename would rather not be handed.
_FOLDER_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def clean_folder(label: str) -> str:
    """Validate a folder label and return it normalised. `""` is the root.

    **The folder is a label, not a location.** A project always lives at
    `<workspace>/projects/<id>/`; this string only says where the *list page* draws
    it. Nesting the workspace directories was considered and rejected in the M3
    plan: all 24 routes take `{project_id}`, so nesting would make that a path and
    hand `web/media.py` — the one security-critical module, where M2 Task 2 found
    `project_id` is separately attacker-controlled — a traversal surface it does not
    have today. A project also holds 1-3 GB of intermediates, and re-filing one
    should be editing a string rather than moving gigabytes.

    Because it is *shaped* like a path, some later code will eventually be careless
    with it. So the shape is refused before it is stored: no leading `/`, no `..` or
    `.` level, no empty level, no backslash, no control character. What survives is
    one or more non-empty `/`-separated levels, each trimmed of surrounding space.
    """
    cleaned = label.strip()
    if not cleaned:
        return ""
    if _FOLDER_CONTROL.search(cleaned):
        raise ValueError("A folder name cannot contain control characters.")
    if "\\" in cleaned:
        raise ValueError("Separate folder levels with / rather than \\.")
    if cleaned.startswith("/"):
        raise ValueError("A folder name is a label, not a path: it cannot start with /.")
    levels = [level.strip() for level in cleaned.split("/")]
    if not all(levels):
        raise ValueError("A folder name cannot have an empty level: no doubled or trailing /.")
    if any(level in {".", ".."} for level in levels):
        raise ValueError("A folder name cannot contain a . or .. level.")
    return "/".join(levels)


class Aspect(StrEnum):
    WIDE = "wide"
    VERTICAL = "vertical"


class Status(StrEnum):
    NEW = "new"
    SCRIPT_READY = "script_ready"
    VOICED = "voiced"
    STORYBOARD_READY = "storyboard_ready"
    PREVIEW_READY = "preview_ready"
    RENDERED = "rendered"


class VisualKind(StrEnum):
    AUTO = "auto"
    STOCK_VIDEO = "stock_video"
    STOCK_PHOTO = "stock_photo"
    AI_IMAGE = "ai_image"


class Motion(StrEnum):
    PAN = "pan"
    ZOOM = "zoom"
    NONE = "none"


class WordTiming(BaseModel):
    word: str
    start_s: float
    end_s: float


class AssetRef(BaseModel):
    provider: str
    source_id: str
    source_url: str
    local_path: str  # always relative to the project folder
    #: The provider's own thumbnail, carried over from `StockResult`. It is the only
    #: picture of a candidate that was offered but never downloaded (`local_path`
    #: empty), which is what the storyboard draws. Empty for a generated image, and
    #: for every `project.json` written before this field existed — hence the default.
    preview_url: str = ""
    width: int
    height: int
    duration_s: float | None = None
    attribution: str = ""
    license: str = ""


class StockResult(BaseModel):
    provider: str
    source_id: str
    source_url: str
    preview_url: str
    download_url: str
    width: int
    height: int
    duration_s: float | None = None
    #: Whatever words the library said this result is about, lowercased: its own
    #: tags, its alt text, the words in its URL slug. `pipeline.ranking` is the only
    #: reader, and it is the only free signal there is about what the clip *shows* —
    #: so a provider that has none leaves this empty rather than inventing any.
    tags: list[str] = Field(default_factory=list)
    attribution: str = ""
    license: str = ""


class SceneVisual(BaseModel):
    query: str
    #: Further searches for the same scene, most specific first, written by the same
    #: script call that wrote `query`. The visuals stage climbs them only when the
    #: one before it came back thin — see `pipeline.ranking.query_ladder`. Empty on
    #: every project written before M3 Task 12, which is why the ladder can still
    #: derive a last rung from `query` alone.
    alt_queries: list[str] = Field(default_factory=list)
    kind: VisualKind = VisualKind.AUTO
    chosen: AssetRef | None = None
    candidates: list[AssetRef] = Field(default_factory=list)
    motion: Motion = Motion.PAN
    crop_focus_x: float = Field(0.5, ge=0.0, le=1.0)
    trim_start_s: float = 0.0


class Scene(BaseModel):
    id: str
    narration: str
    visual: SceneVisual
    audio_path: str | None = None
    duration_s: float | None = None
    words: list[WordTiming] = Field(default_factory=list)
    #: Which of the template's `structure` beats this scene was written for, as
    #: `script.assign_beats` mapped it. `None` on projects written before M3 Task
    #: 22 — and on those the Short selection is left exactly as it was found.
    beat: str | None = None
    #: On the vertical cut. The template's `short_beats` set this when the script
    #: was written; a person can override it, and then `short_pinned` says so.
    in_short: bool = True
    #: A human touched the `in_short` tick. Nothing in the pipeline may overwrite
    #: it after that: the template's beat map is a *guess*, and a re-run that
    #: silently re-ticked a scene would only be found out after publishing.
    short_pinned: bool = False
    locked: bool = False
    error: str | None = None


class Approvals(BaseModel):
    script: datetime | None = None
    storyboard: datetime | None = None
    preview: datetime | None = None


class OutputSpec(BaseModel):
    aspect: Aspect
    width: int
    height: int
    scene_ids: list[str] = Field(default_factory=list)
    video_path: str | None = None


class MusicSelection(BaseModel):
    """Which track sits under this project's narration, and how it is placed.

    Every field is an **override**, and the empty value means "no opinion": `""` for
    the track and the mood, `None` for the levels. That is what keeps the four levels
    of control in `docs/audio-design.md` in order — the template's `music_mood` and
    `config.yaml`'s levels apply until someone overrides them here or at gate 3 — and
    it is why a project written before this field existed loads unchanged.

    `enabled=False` is the only way to say "no music on this one" as a decision
    rather than an accident; an empty `assets/music/` says the same thing by default.
    """

    enabled: bool = True
    #: A library key — `music/calm/rain.mp3`. Empty means "pick one from the mood".
    track_key: str = ""
    #: Overrides the template's `music_mood`. Empty means the template decides.
    mood: str = ""
    #: How loud the bed sits, in dB. `None` means `Settings.music_volume_db`.
    volume_db: float | None = None
    #: How far it drops under speech, in dB. `None` means `Settings.duck_amount_db`.
    duck_db: float | None = None
    #: Whether this project's transition and beat effects play. `None` means
    #: `Settings.sfx_enabled` decides — the same "no opinion" rule as the levels
    #: above, and the reason gate 3 can turn effects off for one video without
    #: touching `config.yaml` for every other one.
    sfx_enabled: bool | None = None


class Project(BaseModel):
    id: str
    topic: str
    template: str
    language: str = "en"
    voice: str = "af_heart"
    target_minutes: float = 2.0
    #: Where the project list files this project — `"tech/office-basics"`, or `""`
    #: for the root. A **label**, validated by `clean_folder`; see its docstring for
    #: why the workspace directories are deliberately not nested. It feeds no stage
    #: fingerprint: filing a project changes nothing rendered, and staling
    #: `script:all` would let the next run replace `scenes` wholesale (M3 Task 22).
    #: Absent from every `project.json` written before M3 Task 23 — hence the default.
    folder: str = ""
    created_at: datetime
    # No stored `status`: it is derived from the stage cache by runner.derive_status()
    # so it can never drift from the artifacts on disk (spec 4.4).
    approvals: Approvals = Field(default_factory=Approvals)
    music: MusicSelection = Field(default_factory=MusicSelection)
    scenes: list[Scene] = Field(default_factory=list)
    outputs: dict[Aspect, OutputSpec] = Field(default_factory=dict)

    @field_validator("folder")
    @classmethod
    def _validated_folder(cls, value: str) -> str:
        return clean_folder(value)

    def scene_by_id(self, sid: str) -> Scene:
        for scene in self.scenes:
            if scene.id == sid:
                return scene
        raise KeyError(sid)

    def next_scene_id(self) -> str:
        numbers = [int(s.id[1:]) for s in self.scenes if s.id[1:].isdigit()]
        return f"s{max(numbers) + 1:02d}" if numbers else "s01"
