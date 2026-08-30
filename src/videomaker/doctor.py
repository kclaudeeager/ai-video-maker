import shutil
import sys
from dataclasses import dataclass

from videomaker.config import Settings
from videomaker.media.ffmpeg import FFmpegCaps

FFMPEG_LIBASS_FIX = (
    "install an FFmpeg build with libass — Debian/Ubuntu: 'sudo apt install ffmpeg'; "
    "macOS: 'brew reinstall ffmpeg' (standard bottles include the 'subtitles' filter)"
)
SETUP_FIX = "uv run videomaker setup"
MODEL_FILES = ("kokoro-v1.0.onnx", "voices-v1.0.bin")
MIN_FREE_GB = 20


@dataclass(frozen=True)
class CheckResult:
    name: str
    level: str  # "ok" | "warn" | "fail"
    detail: str
    fix: str = ""


def _check_python() -> CheckResult:
    v = sys.version_info
    if (v.major, v.minor) == (3, 12):
        return CheckResult("python", "ok", f"{v.major}.{v.minor}.{v.micro}")
    return CheckResult(
        "python", "fail", f"running {v.major}.{v.minor}.{v.micro}, need 3.12.x",
        fix="uv sync   # respects .python-version",
    )


def _check_ffmpeg(caps: FFmpegCaps) -> list[CheckResult]:
    if not caps.installed:
        return [
            CheckResult("ffmpeg", "fail", "not found on PATH",
                        fix="sudo apt install ffmpeg   # (brew install ffmpeg on macOS)")
        ]
    results = [CheckResult("ffmpeg", "ok", caps.version)]
    if caps.has_subtitles_filter:
        results.append(CheckResult("ffmpeg subtitles filter", "ok", "libass available"))
    else:
        results.append(
            CheckResult(
                "ffmpeg subtitles filter", "fail",
                "this ffmpeg build lacks libass — captions cannot be burned",
                fix=FFMPEG_LIBASS_FIX,
            )
        )
    if not caps.has_ffprobe:
        results.append(
            CheckResult("ffprobe", "fail", "not found on PATH",
                        fix="sudo apt install ffmpeg   # (brew install ffmpeg on macOS)")
        )
    # Detection is real, but nothing in the pipeline encodes with it yet: `assemble`
    # and `render` both hardcode libx264, and hardware fast-render mode is M3 work
    # (spike follow-up 8). Say what is true rather than promising faster renders.
    results.append(
        CheckResult(
            "hardware encoder", "ok" if caps.hw_encoder else "warn",
            f"{caps.hw_encoder} detected; renders use libx264 (CPU) until "
            "fast-render mode lands in M3" if caps.hw_encoder
            else "none detected; libx264 (CPU) only",
        )
    )
    return results


def _check_disk(settings: Settings) -> CheckResult:
    target = settings.workspace_dir if settings.workspace_dir.exists() else settings.workspace_dir.parent
    if not target.exists():
        target = target.parent
    free_gb = shutil.disk_usage(target).free / 1e9
    if free_gb >= MIN_FREE_GB:
        return CheckResult("disk space", "ok", f"{free_gb:.0f} GB free")
    return CheckResult(
        "disk space", "warn", f"only {free_gb:.0f} GB free (want >= {MIN_FREE_GB} GB)",
        fix="uv run videomaker clean --keep-outputs   # (available from M3)",
    )


def _check_models(settings: Settings) -> CheckResult:
    missing = [f for f in MODEL_FILES if not (settings.models_dir / f).exists()]
    if not missing:
        return CheckResult("kokoro model files", "ok", str(settings.models_dir))
    return CheckResult(
        "kokoro model files", "warn", f"missing: {', '.join(missing)}", fix=SETUP_FIX
    )


def _check_api_keys(settings: Settings) -> list[CheckResult]:
    results = []
    if settings.groq_api_key or settings.gemini_api_key:
        results.append(CheckResult("LLM API key", "ok", "groq and/or gemini configured"))
    else:
        results.append(
            CheckResult(
                "LLM API key", "warn", "neither GROQ_API_KEY nor GEMINI_API_KEY set",
                fix="cp .env.example .env and fill in at least one (both are free)",
            )
        )
    if settings.pexels_api_key:
        results.append(CheckResult("Pexels API key", "ok", "configured"))
    else:
        results.append(
            CheckResult("Pexels API key", "warn", "PEXELS_API_KEY not set",
                        fix="free key at https://www.pexels.com/api/")
        )
    return results


def run_checks(settings: Settings, caps: FFmpegCaps) -> list[CheckResult]:
    results = [_check_python()]
    results.extend(_check_ffmpeg(caps))
    results.append(_check_disk(settings))
    results.append(_check_models(settings))
    results.extend(_check_api_keys(settings))
    return results
