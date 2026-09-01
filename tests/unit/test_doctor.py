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


def test_a_listed_but_unopenable_encoder_is_not_reported_as_one(tmp_path):
    """M1 defect 8, twice over.

    First shape: doctor claimed a hardware encoder was "available for fast renders"
    while `assemble` and `render` both hardcoded libx264. M3 Task 18 built the fast
    render, so the promissory wording is gone.

    Second shape, and the one this machine actually has: `ffmpeg -encoders` lists
    `h264_qsv` and no session will open, because the build carries the wrapper and
    the box has no MFX runtime. `GOOD_CAPS` is exactly that state — listed, unusable
    — and calling it "ok" is how `--fast` would encode on the CPU while the tool
    said it was on the GPU.
    """
    settings = Settings(workspace_dir=tmp_path, models_dir=tmp_path / "models")
    check = _by_name(run_checks(settings, GOOD_CAPS), "hardware encoder")
    assert check.level == "warn"
    assert "h264_qsv" in check.detail
    assert "libx264" in check.detail
    assert check.fix
    assert "fast renders" not in check.detail
    assert "M3" not in check.detail


def test_an_encoder_that_opens_is_reported_as_usable(tmp_path):
    settings = Settings(workspace_dir=tmp_path, models_dir=tmp_path / "models")
    caps = FFmpegCaps(True, "ffmpeg version 6.1.1", True, "h264_vaapi", True, "h264_vaapi")
    check = _by_name(run_checks(settings, caps), "hardware encoder")
    assert check.level == "ok"
    assert "h264_vaapi" in check.detail
    assert "--fast" in check.detail
    # libx264 is still the default, and doctor may not imply otherwise.
    assert "libx264" in check.detail


def test_no_hardware_encoder_still_warns(tmp_path):
    settings = Settings(workspace_dir=tmp_path, models_dir=tmp_path / "models")
    caps = FFmpegCaps(True, "ffmpeg version 6.1.1", True, "", True)
    check = _by_name(run_checks(settings, caps), "hardware encoder")
    assert check.level == "warn"
    assert "libx264" in check.detail


# ------------------------------------------------------- caption fonts (M3 21)


def test_the_caption_font_check_names_the_file_libass_would_load(tmp_path, monkeypatch):
    from videomaker.media import fonts

    monkeypatch.setattr(
        fonts, "resolve_family", lambda family: fonts.ResolvedFamily("DejaVu Sans", "/x.ttf", True)
    )
    monkeypatch.setattr(fonts, "fontconfig_charsets", lambda: "0-10ffff\n")
    fonts.clear_font_cache()
    settings = Settings(workspace_dir=tmp_path, models_dir=tmp_path / "models")

    check = _by_name(run_checks(settings, GOOD_CAPS), "caption font")

    assert check.level == "ok"
    assert "/x.ttf" in check.detail
    fonts.clear_font_cache()


def test_a_substituted_caption_font_warns_rather_than_passing_quietly(tmp_path, monkeypatch):
    """fontconfig never fails a match, so a missing family looks fine until this."""
    from videomaker.media import fonts

    monkeypatch.setattr(
        fonts, "resolve_family", lambda family: fonts.ResolvedFamily("Noto Sans", "/n.ttf", False)
    )
    monkeypatch.setattr(fonts, "fontconfig_charsets", lambda: "0-10ffff\n")
    fonts.clear_font_cache()
    settings = Settings(workspace_dir=tmp_path, models_dir=tmp_path / "models")

    check = _by_name(run_checks(settings, GOOD_CAPS), "caption font")

    assert check.level == "warn"
    assert "Noto Sans" in check.detail
    assert check.fix
    fonts.clear_font_cache()


def test_a_script_no_installed_font_can_draw_fails_with_the_font_fix(tmp_path, monkeypatch):
    """The tofu case: correct audio, unreadable captions, and nothing says so."""
    from videomaker.media import fonts

    monkeypatch.setattr(fonts, "fontconfig_charsets", lambda: "20-7e\n2010-2027\n")
    fonts.clear_font_cache()
    settings = Settings(workspace_dir=tmp_path, models_dir=tmp_path / "models")

    check = _by_name(run_checks(settings, GOOD_CAPS), "caption script coverage")

    assert check.level == "fail"
    assert "Spanish" in check.detail
    assert "apt install" in check.fix
    fonts.clear_font_cache()


def test_every_offered_script_drawable_passes(tmp_path, monkeypatch):
    from videomaker.media import fonts

    monkeypatch.setattr(fonts, "fontconfig_charsets", lambda: "0-10ffff\n")
    fonts.clear_font_cache()
    settings = Settings(workspace_dir=tmp_path, models_dir=tmp_path / "models")

    check = _by_name(run_checks(settings, GOOD_CAPS), "caption script coverage")

    assert check.level == "ok"
    assert "English" in check.detail
    fonts.clear_font_cache()


def test_an_unprobeable_machine_warns_instead_of_claiming_coverage(tmp_path, monkeypatch):
    from videomaker.media import fonts

    monkeypatch.setattr(fonts, "fontconfig_charsets", lambda: None)
    fonts.clear_font_cache()
    settings = Settings(workspace_dir=tmp_path, models_dir=tmp_path / "models")

    check = _by_name(run_checks(settings, GOOD_CAPS), "caption script coverage")

    assert check.level == "warn"
    assert "fontconfig" in check.detail
    fonts.clear_font_cache()
