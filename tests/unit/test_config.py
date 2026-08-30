from pathlib import Path

from videomaker.config import Settings, load_settings


def test_defaults_when_no_config_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = load_settings()
    assert settings.workspace_dir == Path("workspace")
    assert settings.models_dir == Path.home() / ".cache" / "ai-video-maker" / "models"


def test_config_yaml_overrides_paths(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("paths:\n  workspace_dir: /tmp/ws\n  models_dir: /tmp/models\n")
    settings = load_settings(cfg)
    assert settings.workspace_dir == Path("/tmp/ws")
    assert settings.models_dir == Path("/tmp/models")


def test_api_key_read_from_plain_env_var(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gk-test")
    assert Settings().groq_api_key == "gk-test"
