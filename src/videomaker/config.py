from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from videomaker.media.audio import DEFAULT_DUCK_DB, DEFAULT_VOLUME_DB

DEFAULT_CONFIG_FILE = Path("config.yaml")


class Audience(StrEnum):
    """Who this server is for, which decides what it mounts.

    Not a role and not a permission: a *deployment* choice. A `READER` app never
    registers the studio routers, so it cannot create a project, run a stage,
    approve a gate or start a render — the code that does those things is not
    there. That is what lets a household share a library without the tool growing
    accounts, and it is why this is one setting rather than a permission system.
    """

    STUDIO = "studio"
    READER = "reader"

#: The `audio:` keys `config.yaml` may set. Anything else there is ignored.
AUDIO_LEVELS = frozenset({"music_volume_db", "duck_amount_db"})
#: ...and the switches. Kept apart from the levels because they are read as bools:
#: `float("false")` is a crash, and a config file must not be able to cause one.
AUDIO_TOGGLES = frozenset({"sfx_enabled", "transition_sfx_enabled"})

#: The `visuals:` switches, mapped from the config file's key to the setting. The
#: names differ because the section already says "visuals" and the setting has to
#: say it for itself; anything not listed here is ignored, as in `audio:`.
VISUALS_TOGGLES: dict[str, str] = {"rerank_enabled": "visual_rerank_enabled"}

#: The `render:` switches, mapped the same way and for the same reason: the section
#: already says "render", and the setting has to say it for itself.
RENDER_TOGGLES: dict[str, str] = {"fast_mode": "render_fast_mode"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    #: What this server is for. `studio` is everything; `reader` mounts the
    #: library and nothing that can produce. See `Audience`.
    audience: Audience = Audience.STUDIO

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

    #: The Gemini Flash visual re-rank (`docs/visual-search-design.md` item 4).
    #: **Off by default, deliberately.** It spends the same 240-a-day Gemini budget
    #: the script stage's LLM fallback draws on, and a feature that quietly eats
    #: someone's daily cap is worse than one that does nothing. M3 Task 14's harness
    #: is what decides whether it earns its quota.
    visual_rerank_enabled: bool = False

    #: Encode with the machine's hardware H.264 encoder instead of libx264.
    #: **Off by default, and libx264 stays the quality path.** Measured here on a
    #: 116 s 1080p cut: `h264_vaapi` halves the segment encode (37 s -> 19 s) and
    #: cuts the whole run's CPU time roughly fourfold, for SSIM 0.9934 against
    #: libx264's 0.9952 and a fifth more bytes. Worth asking for; not worth
    #: assuming. Where no hardware encoder will open — which includes machines
    #: whose `ffmpeg -encoders` cheerfully lists one — the stage says so and
    #: encodes on the CPU rather than failing.
    render_fast_mode: bool = False

    groq_api_key: str = Field(default="", validation_alias=AliasChoices("GROQ_API_KEY"))
    gemini_api_key: str = Field(default="", validation_alias=AliasChoices("GEMINI_API_KEY"))
    pexels_api_key: str = Field(default="", validation_alias=AliasChoices("PEXELS_API_KEY"))
    cloudflare_account_id: str = Field(
        default="", validation_alias=AliasChoices("CLOUDFLARE_ACCOUNT_ID")
    )
    cloudflare_api_token: str = Field(
        default="", validation_alias=AliasChoices("CLOUDFLARE_API_TOKEN")
    )

    #: Signs the reader's preference cookie. Empty — the default — means a secret
    #: generated per process, so a restart simply forgets everyone's preference
    #: and nobody has to invent a value to run the tool locally. Set it to keep
    #: preferences across restarts. It is not a session key and guards nothing
    #: but a mode and a language; see `web/routes/library.py`.
    reader_cookie_secret: str = ""

    #: `music_sources:` from `config.yaml`, as written — the catalogues
    #: `videomaker music fetch` asks. Raw for the same reason `voice_providers`
    #: is: this module cannot import the one that validates them. Empty means the
    #: single blessed default (`musicfetch.OPENVERSE`), so a fresh clone can fetch
    #: without configuring anything.
    music_sources: list[dict[str, Any]] = Field(default_factory=list)

    #: `voice_providers:` from `config.yaml`, as written. Kept raw here because this
    #: module cannot import `providers.tts.http_api` (it imports us); the entries
    #: are validated into `HTTPTTSConfig` where they are used. Any name listed
    #: becomes selectable in `provider_chains["tts"]` — see `providers.get_provider`.
    voice_providers: list[dict[str, Any]] = Field(default_factory=list)

    provider_chains: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "llm": ["groq", "gemini"],
            "tts": ["kokoro"],
            "stt": ["fasterwhisper"],
            "stock": ["pexels"],
            "image": ["cloudflare"],
            # Only ever built when `visual_rerank_enabled` is on, so listing it
            # costs a run that leaves the switch alone exactly nothing.
            "vision": ["gemini"],
            # The library on disk. Nothing builds it until a reader asks for a
            # work, so a machine that has imported nothing pays no import cost.
            "corpus": ["bible"],
        }
    )


def load_settings(config_file: Path | None = None) -> Settings:
    path = config_file or DEFAULT_CONFIG_FILE
    overrides: dict[str, object] = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text()) or {}
        audience = raw.get("audience")
        if isinstance(audience, str) and audience in set(Audience):
            overrides["audience"] = Audience(audience)
        for key, value in (raw.get("paths") or {}).items():
            if key in {"workspace_dir", "models_dir", "music_dir", "sfx_dir"}:
                overrides[key] = Path(str(value)).expanduser()
        for key, value in (raw.get("audio") or {}).items():
            if key in AUDIO_LEVELS:
                overrides[key] = float(value)
            elif key in AUDIO_TOGGLES:
                overrides[key] = bool(value)
        for section, toggles in (("visuals", VISUALS_TOGGLES), ("render", RENDER_TOGGLES)):
            for key, value in (raw.get(section) or {}).items():
                setting = toggles.get(key)
                if setting is not None:
                    overrides[setting] = bool(value)
        chains = {
            str(kind): [str(name) for name in names]
            for kind, names in (raw.get("providers") or {}).items()
        }
        if chains:
            overrides["provider_chains"] = Settings().provider_chains | chains
        sources = raw.get("music_sources")
        if isinstance(sources, list):
            overrides["music_sources"] = [row for row in sources if isinstance(row, dict)]
        voices = raw.get("voice_providers")
        if isinstance(voices, list):
            overrides["voice_providers"] = [entry for entry in voices if isinstance(entry, dict)]
    return Settings(**overrides)
