from pathlib import Path

import yaml
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_CONFIG_FILE = Path("config.yaml")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    workspace_dir: Path = Path("workspace")
    models_dir: Path = Path.home() / ".cache" / "ai-video-maker" / "models"
    music_dir: Path = Path("assets/music")

    groq_api_key: str = Field(default="", validation_alias=AliasChoices("GROQ_API_KEY"))
    gemini_api_key: str = Field(default="", validation_alias=AliasChoices("GEMINI_API_KEY"))
    pexels_api_key: str = Field(default="", validation_alias=AliasChoices("PEXELS_API_KEY"))
    cloudflare_account_id: str = Field(
        default="", validation_alias=AliasChoices("CLOUDFLARE_ACCOUNT_ID")
    )
    cloudflare_api_token: str = Field(
        default="", validation_alias=AliasChoices("CLOUDFLARE_API_TOKEN")
    )


def load_settings(config_file: Path | None = None) -> Settings:
    path = config_file or DEFAULT_CONFIG_FILE
    overrides: dict[str, Path] = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text()) or {}
        for key, value in (raw.get("paths") or {}).items():
            if key in {"workspace_dir", "models_dir", "music_dir"}:
                overrides[key] = Path(str(value)).expanduser()
    return Settings(**overrides)
