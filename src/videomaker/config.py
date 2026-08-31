from pathlib import Path

import yaml
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from videomaker.media.audio import DEFAULT_DUCK_DB, DEFAULT_VOLUME_DB

DEFAULT_CONFIG_FILE = Path("config.yaml")

#: The `audio:` keys `config.yaml` may set. Anything else there is ignored.
AUDIO_LEVELS = frozenset({"music_volume_db", "duck_amount_db"})
#: ...and the switches. Kept apart from the levels because they are read as bools:
#: `float("false")` is a crash, and a config file must not be able to cause one.
AUDIO_TOGGLES = frozenset({"sfx_enabled", "transition_sfx_enabled"})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    workspace_dir: Path = Path("workspace")
    models_dir: Path = Path.home() / ".cache" / "ai-video-maker" / "models"
    music_dir: Path = Path("assets/music")
    sfx_dir: Path = Path("assets/sfx")

    #: How loud the music bed sits before ducking, and how far it drops under speech.
    #: Defaults live in `media/audio.py`, which is what reads them.
    music_volume_db: float = DEFAULT_VOLUME_DB
    duck_amount_db: float = DEFAULT_DUCK_DB

    #: The sound-effect layer, and the transitions on scene cuts within it. On by
    #: default and yet silent on a fresh clone: effects come from the user's own
    #: `assets/sfx/`, which the project ships empty and always will. Switching them
    #: off is for someone who *has* a library and wants one video without it.
    sfx_enabled: bool = True
    transition_sfx_enabled: bool = True

    groq_api_key: str = Field(default="", validation_alias=AliasChoices("GROQ_API_KEY"))
    gemini_api_key: str = Field(default="", validation_alias=AliasChoices("GEMINI_API_KEY"))
    pexels_api_key: str = Field(default="", validation_alias=AliasChoices("PEXELS_API_KEY"))
    cloudflare_account_id: str = Field(
        default="", validation_alias=AliasChoices("CLOUDFLARE_ACCOUNT_ID")
    )
    cloudflare_api_token: str = Field(
        default="", validation_alias=AliasChoices("CLOUDFLARE_API_TOKEN")
    )

    provider_chains: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "llm": ["groq", "gemini"],
            "tts": ["kokoro"],
            "stt": ["fasterwhisper"],
            "stock": ["pexels"],
            "image": ["cloudflare"],
        }
    )


def load_settings(config_file: Path | None = None) -> Settings:
    path = config_file or DEFAULT_CONFIG_FILE
    overrides: dict[str, object] = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text()) or {}
        for key, value in (raw.get("paths") or {}).items():
            if key in {"workspace_dir", "models_dir", "music_dir", "sfx_dir"}:
                overrides[key] = Path(str(value)).expanduser()
        for key, value in (raw.get("audio") or {}).items():
            if key in AUDIO_LEVELS:
                overrides[key] = float(value)
            elif key in AUDIO_TOGGLES:
                overrides[key] = bool(value)
        chains = {
            str(kind): [str(name) for name in names]
            for kind, names in (raw.get("providers") or {}).items()
        }
        if chains:
            overrides["provider_chains"] = Settings().provider_chains | chains
    return Settings(**overrides)
