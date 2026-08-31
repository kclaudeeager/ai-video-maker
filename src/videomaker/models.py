from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


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
    attribution: str = ""
    license: str = ""


class SceneVisual(BaseModel):
    query: str
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


class Project(BaseModel):
    id: str
    topic: str
    template: str
    language: str = "en"
    voice: str = "af_heart"
    target_minutes: float = 2.0
    created_at: datetime
    # No stored `status`: it is derived from the stage cache by runner.derive_status()
    # so it can never drift from the artifacts on disk (spec 4.4).
    approvals: Approvals = Field(default_factory=Approvals)
    music: MusicSelection = Field(default_factory=MusicSelection)
    scenes: list[Scene] = Field(default_factory=list)
    outputs: dict[Aspect, OutputSpec] = Field(default_factory=dict)

    def scene_by_id(self, sid: str) -> Scene:
        for scene in self.scenes:
            if scene.id == sid:
                return scene
        raise KeyError(sid)

    def next_scene_id(self) -> str:
        numbers = [int(s.id[1:]) for s in self.scenes if s.id[1:].isdigit()]
        return f"s{max(numbers) + 1:02d}" if numbers else "s01"
