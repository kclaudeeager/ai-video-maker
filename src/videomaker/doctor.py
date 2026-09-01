import shutil
import sys
from dataclasses import dataclass

from videomaker.audio import LIBRARY_FILENAME, scan
from videomaker.config import Settings
from videomaker.languages import OFFERED_CODES
from videomaker.media import fonts
from videomaker.media.ffmpeg import FFmpegCaps

FFMPEG_LIBASS_FIX = (
    "install an FFmpeg build with libass — Debian/Ubuntu: 'sudo apt install ffmpeg'; "
    "macOS: 'brew reinstall ffmpeg' (standard bottles include the 'subtitles' filter)"
)
SETUP_FIX = "uv run videomaker setup"
HW_DRIVER_FIX = (
    "install the driver behind it and check /dev/dri — Debian/Ubuntu Intel: "
    "'sudo apt install intel-media-va-driver-non-free vainfo' and add yourself to "
    "the 'render' group; then 'vainfo' should list H264 profiles. Nothing is broken "
    "without it: renders stay on libx264."
)
FONTCONFIG_FIX = (
    "install fontconfig so libass can find fonts — Debian/Ubuntu: "
    "'sudo apt install fontconfig'"
)
LIBRARY_FIX = (
    "record them in assets/music/library.yaml (title, artist, licence, attribution, "
    "source_url) so the video description can credit them"
)
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
    results.append(_check_hw_encoder(caps))
    return results


def _check_hw_encoder(caps: FFmpegCaps) -> CheckResult:
    """Three answers, because there are three states and M0 only reported two.

    M1 defect 8 was this check claiming a hardware encoder was "available for fast
    renders" when nothing used one; M3 Task 18 built the thing it was promising. The
    state it never had a word for is the middle one, and it is the state of the
    machine this was written on: `ffmpeg -encoders` lists `h264_qsv`, and no session
    will open because there is no MFX runtime behind it. Reporting that as a working
    encoder is how `--fast` would end up quietly encoding on the CPU.
    """
    if caps.fast_encoder:
        return CheckResult(
            "hardware encoder", "ok",
            f"{caps.fast_encoder} opens; `videomaker run --fast` encodes with it. "
            "libx264 (CPU) stays the default quality path.",
        )
    if caps.hw_encoder:
        return CheckResult(
            "hardware encoder", "warn",
            f"this ffmpeg lists {caps.hw_encoder}, but no hardware encoder on this "
            "machine would open; --fast would encode with libx264 (CPU)",
            fix=HW_DRIVER_FIX,
        )
    return CheckResult(
        "hardware encoder", "warn", "none detected; libx264 (CPU) only"
    )


def _check_caption_font() -> CheckResult:
    """Is the family the caption styles name actually installed?

    fontconfig never fails a match: ask for a family nobody has and it hands back
    its best substitute with no error and no log line, so a caption font can go
    missing and every render still succeeds — looking different. `exact` is the
    only way to tell the two apart, which is why this is a check and not a note.
    """
    resolved = fonts.resolve_family(fonts.CAPTION_FONT)
    if resolved is None:
        return CheckResult(
            "caption font", "warn",
            f"could not ask fontconfig for {fonts.CAPTION_FONT}",
            fix=FONTCONFIG_FIX,
        )
    if resolved.exact:
        return CheckResult(
            "caption font", "ok", f"{resolved.family} -> {resolved.path}"
        )
    return CheckResult(
        "caption font", "warn",
        f"{fonts.CAPTION_FONT} is not installed; libass would substitute "
        f"{resolved.family} ({resolved.path})",
        fix=fonts.FONT_PACKAGE_FIX,
    )


def _check_caption_scripts() -> CheckResult:
    """Can anything installed draw the scripts of the languages we offer?

    libass falls back per glyph through fontconfig, so this asks about every
    installed font rather than about the styled family alone — verified by
    rendering a frame, see `docs/language-support.md`. A script nothing can draw
    is the tofu case: correct audio, unreadable captions, no error anywhere.
    """
    checks = fonts.script_checks()
    by_code = {check.code: check for check in checks}
    wanted = [by_code[code] for code in OFFERED_CODES if code in by_code]
    if not any(check.probed for check in checks):
        return CheckResult(
            "caption script coverage", "warn",
            "no fontconfig to ask; caption glyph coverage is unverified",
            fix=FONTCONFIG_FIX,
        )
    broken = [check for check in wanted if not check.renders]
    if not broken:
        names = ", ".join(check.name for check in wanted)
        return CheckResult("caption script coverage", "ok", f"{names} all draw")
    detail = "; ".join(f"{check.name} cannot draw {check.missing}" for check in broken)
    return CheckResult(
        "caption script coverage", "fail",
        f"captions would burn in as tofu boxes — {detail}",
        fix=fonts.FONT_PACKAGE_FIX,
    )


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


def _check_audio_library(settings: Settings) -> CheckResult:
    """What is in the user's own music/SFX library, and whether it can be credited.

    An empty library is `ok`, not a warning: the project ships no audio
    (`docs/audio-design.md`), so empty is the state of every fresh clone and the
    pipeline renders narration only. The one thing worth nagging about is a file
    with no `library.yaml` entry — usable, but nothing can credit it, and M5
    generates the attribution block from that record.

    `probe=None` keeps this off ffprobe: `doctor` is run to get a quick answer,
    not to measure a hundred tracks.
    """
    library = scan(settings.music_dir, settings.sfx_dir, probe=None)
    if not library:
        return CheckResult(
            "audio library", "ok",
            f"empty ({settings.music_dir}, {settings.sfx_dir}) — renders are narration only",
        )
    summary = (
        f"{len(library.music())} track(s) in {', '.join(library.moods()) or 'no mood'}; "
        f"{len(library.sfx())} sfx in {', '.join(library.roles()) or 'no role'}"
    )
    missing = library.unattributed()
    if not missing:
        return CheckResult("audio library", "ok", f"{summary}; all recorded in {LIBRARY_FILENAME}")
    return CheckResult(
        "audio library", "warn",
        f"{summary}; {len(missing)} not recorded in {LIBRARY_FILENAME}",
        fix=LIBRARY_FIX,
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
    results.append(_check_caption_font())
    results.append(_check_caption_scripts())
    results.append(_check_disk(settings))
    results.append(_check_audio_library(settings))
    results.append(_check_models(settings))
    results.extend(_check_api_keys(settings))
    return results
