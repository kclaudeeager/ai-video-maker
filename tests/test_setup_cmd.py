from videomaker.config import Settings
from videomaker.setup_cmd import KOKORO_MODEL_URL, KOKORO_VOICES_URL, ensure_models


def test_ensure_models_downloads_missing_files(tmp_path):
    calls = []

    def fake_downloader(url, dest, **kwargs):
        calls.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x")
        return dest

    settings = Settings(workspace_dir=tmp_path, models_dir=tmp_path / "models")
    paths = ensure_models(settings, downloader=fake_downloader)
    assert sorted(calls) == sorted([KOKORO_MODEL_URL, KOKORO_VOICES_URL])
    assert all(p.exists() for p in paths)


def test_ensure_models_skips_existing_files(tmp_path):
    models = tmp_path / "models"
    models.mkdir(parents=True)
    (models / "kokoro-v1.0.onnx").write_bytes(b"x")
    (models / "voices-v1.0.bin").write_bytes(b"x")
    calls = []

    def fake_downloader(url, dest, **kwargs):
        calls.append(url)
        return dest

    settings = Settings(workspace_dir=tmp_path, models_dir=models)
    ensure_models(settings, downloader=fake_downloader)
    assert calls == []
