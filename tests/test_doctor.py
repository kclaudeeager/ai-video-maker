from videomaker.config import Settings
from videomaker.doctor import FFMPEG_LIBASS_FIX, run_checks
from videomaker.media.ffmpeg import FFmpegCaps

GOOD_CAPS = FFmpegCaps(True, "ffmpeg version 6.1.1", True, "h264_qsv", True)
NO_LIBASS_CAPS = FFmpegCaps(True, "ffmpeg version 6.1.1", False, "h264_qsv", True)


def _by_name(results, name):
    return next(r for r in results if r.name == name)


def test_missing_subtitles_filter_fails_with_remediation(tmp_path):
    settings = Settings(workspace_dir=tmp_path, models_dir=tmp_path / "models")
    results = run_checks(settings, NO_LIBASS_CAPS)
    check = _by_name(results, "ffmpeg subtitles filter")
    assert check.level == "fail"
    assert check.fix == FFMPEG_LIBASS_FIX
    assert "apt install ffmpeg" in check.fix


def test_good_ffmpeg_passes(tmp_path):
    settings = Settings(workspace_dir=tmp_path, models_dir=tmp_path / "models")
    results = run_checks(settings, GOOD_CAPS)
    assert _by_name(results, "ffmpeg subtitles filter").level == "ok"
    assert _by_name(results, "ffmpeg").level == "ok"


def test_missing_models_warns_with_setup_fix(tmp_path):
    settings = Settings(workspace_dir=tmp_path, models_dir=tmp_path / "models")
    results = run_checks(settings, GOOD_CAPS)
    check = _by_name(results, "kokoro model files")
    assert check.level == "warn"
    assert "videomaker setup" in check.fix


def test_present_models_pass(tmp_path):
    models = tmp_path / "models"
    models.mkdir(parents=True)
    (models / "kokoro-v1.0.onnx").write_bytes(b"x")
    (models / "voices-v1.0.bin").write_bytes(b"x")
    settings = Settings(workspace_dir=tmp_path, models_dir=models)
    results = run_checks(settings, GOOD_CAPS)
    assert _by_name(results, "kokoro model files").level == "ok"


def test_no_llm_key_warns(tmp_path, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    settings = Settings(
        workspace_dir=tmp_path, models_dir=tmp_path / "models",
        groq_api_key="", gemini_api_key="",
    )
    results = run_checks(settings, GOOD_CAPS)
    assert _by_name(results, "LLM API key").level == "warn"
