import json
import shutil
import subprocess
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

HW_ENCODER_PREFERENCE = ("h264_qsv", "h264_vaapi", "h264_videotoolbox")

#: How many trailing stderr lines are kept for the error message.
STDERR_TAIL_LINES = 30

#: FFmpeg emits progress every this many seconds of wall time.
STATS_PERIOD_S = 0.01


class FFmpegError(RuntimeError):
    """An ffmpeg/ffprobe invocation exited non-zero."""

    def __init__(self, message: str, *, returncode: int = 0, stderr_tail: str = "") -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr_tail = stderr_tail


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


def _parse_progress_line(line: str) -> float | None:
    """Return output seconds from a ``-progress`` ``out_time_ms=`` line, if any.

    FFmpeg's ``out_time_ms`` is actually microseconds (a long-standing quirk),
    and is ``N/A`` until the first frame is written. The first tick can be
    slightly negative (a start-time offset), so it is clamped to zero.
    """
    key, _, value = line.strip().partition("=")
    if key != "out_time_ms":
        return None
    try:
        return max(0.0, int(value) / 1_000_000)
    except ValueError:
        return None


def run_ffmpeg(
    args: list[str],
    *,
    cwd: Path | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> None:
    """Run ffmpeg with ``args``, raising :class:`FFmpegError` on failure.

    When ``on_progress`` is given, ffmpeg is asked for machine-readable progress
    and the callback receives the number of output seconds encoded so far.
    """
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y"]
    if on_progress is not None:
        cmd += ["-progress", "pipe:1", "-nostats", "-stats_period", str(STATS_PERIOD_S)]
    cmd += args

    tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
    process = subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd is not None else None,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    def drain_stderr() -> None:
        assert process.stderr is not None
        for line in process.stderr:
            tail.append(line.rstrip("\n"))

    reader = threading.Thread(target=drain_stderr, daemon=True)
    reader.start()
    try:
        assert process.stdout is not None
        for line in process.stdout:
            if on_progress is None:
                continue
            seconds = _parse_progress_line(line)
            if seconds is not None:
                on_progress(seconds)
    finally:
        returncode = process.wait()
        reader.join()

    if returncode != 0:
        stderr_tail = "\n".join(tail)
        raise FFmpegError(
            f"ffmpeg exited with status {returncode}:\n{stderr_tail}",
            returncode=returncode,
            stderr_tail=stderr_tail,
        )


def probe_json(path: Path) -> dict:
    """Return ffprobe's full JSON description of ``path``."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        stderr_tail = "\n".join(result.stderr.splitlines()[-STDERR_TAIL_LINES:])
        raise FFmpegError(
            f"ffprobe exited with status {result.returncode} for {path}:\n{stderr_tail}",
            returncode=result.returncode,
            stderr_tail=stderr_tail,
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise FFmpegError(f"ffprobe returned unparseable JSON for {path}") from exc


def probe_duration(path: Path) -> float:
    """Return the duration of ``path`` in seconds."""
    info = probe_json(path)
    candidates = [info.get("format", {}).get("duration")]
    candidates += [stream.get("duration") for stream in info.get("streams", [])]
    for candidate in candidates:
        if candidate not in (None, "N/A"):
            return float(candidate)
    raise FFmpegError(f"ffprobe reported no duration for {path}")


def probe_dimensions(path: Path) -> tuple[int, int]:
    """Return ``(width, height)`` of the first video stream in ``path``."""
    info = probe_json(path)
    for stream in info.get("streams", []):
        if stream.get("codec_type") == "video":
            return int(stream["width"]), int(stream["height"])
    raise FFmpegError(f"ffprobe found no video stream in {path}")
