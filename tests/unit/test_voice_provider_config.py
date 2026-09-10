"""The shipped `config.example.yaml` and the voice-provider example inside it.

Two things have to stay true at once: the example file must load into `Settings`
with every vendor commented out (a fresh clone configures no paid voice), and the
commented example, once uncommented, must be a valid `HTTPTTSConfig` — a worked
example that does not validate is worse than none, because someone will copy it.
"""

from pathlib import Path

from videomaker.config import load_settings
from videomaker.providers.tts.http_api import HTTPTTSConfig

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "config.example.yaml"
ENV_EXAMPLE = REPO / ".env.example"

def test_the_example_loads_with_no_voice_provider_configured():
    settings = load_settings(EXAMPLE)
    assert settings.voice_providers == []
    assert settings.provider_chains["tts"] == ["kokoro"]


def test_the_commented_example_validates_as_a_voice_provider(commented_example):
    (entry,) = commented_example("voice_providers")["voice_providers"]
    cfg = HTTPTTSConfig.model_validate(entry)
    assert cfg.name == "vendor"
    assert cfg.api_key_env == "VOICE_API_KEY"
    assert cfg.cost_per_minute_usd > 0
    assert cfg.languages
    assert cfg.max_concurrency == 1


def test_the_example_names_every_field_of_the_model(commented_example):
    # The example is the tour of the model; a field added to one and not the
    # other is how documentation rots.
    (entry,) = commented_example("voice_providers")["voice_providers"]
    assert set(entry) == set(HTTPTTSConfig.model_fields)


def test_the_env_example_points_at_the_config_field():
    text = ENV_EXAMPLE.read_text()
    assert "# VOICE_API_KEY=" in text
    assert "api_key_env" in text


def test_the_docs_say_it_spends_money():
    doc = (REPO / "docs" / "voice-providers.md").read_text()
    assert "real money" in doc
    assert "budget" in doc.lower()
    readme = (REPO / "README.md").read_text()
    assert "docs/voice-providers.md" in readme
