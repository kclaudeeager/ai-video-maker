import shutil
import subprocess
from dataclasses import dataclass

HW_ENCODER_PREFERENCE = ("h264_qsv", "h264_vaapi", "h264_videotoolbox")


@dataclass(frozen=True)
class FFmpegCaps:
    installed: bool
    version: str
    has_subtitles_filter: bool
    hw_encoder: str  # first available of HW_ENCODER_PREFERENCE, else ""
    has_ffprobe: bool


def _run(args: list[str]) -> str:
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    return result.stdout


def probe_capabilities() -> FFmpegCaps:
    if shutil.which("ffmpeg") is None:
        return FFmpegCaps(False, "", False, "", False)
    version_out = _run(["ffmpeg", "-hide_banner", "-version"])
    version = version_out.splitlines()[0].strip() if version_out else "unknown"
    filters = _run(["ffmpeg", "-hide_banner", "-filters"])
    encoders = _run(["ffmpeg", "-hide_banner", "-encoders"])
    hw_encoder = next((name for name in HW_ENCODER_PREFERENCE if name in encoders), "")
    return FFmpegCaps(
        installed=True,
        version=version,
        has_subtitles_filter=" subtitles " in filters,
        hw_encoder=hw_encoder,
        has_ffprobe=shutil.which("ffprobe") is not None,
    )
