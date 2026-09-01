import json
import shutil
import subprocess
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

HW_ENCODER_PREFERENCE = ("h264_qsv", "h264_vaapi", "h264_videotoolbox")

#: The default, and the only *quality* path. Fast mode is opt-in in both directions:
#: nothing picks a hardware encoder unless someone asks for one.
SOFTWARE_ENCODER = "libx264"

#: Constant quantiser for the hardware encoders, measured rather than guessed.
#: On this machine (`h264_vaapi`, 116 s of 1080p30) against libx264 `-crf 18`:
#: qp 18 scores SSIM 0.9941 for 98 MB, qp 20 scores 0.9934 for 70 MB, qp 22 scores
#: 0.9920 for 45 MB — libx264 itself scores 0.9952 for 60 MB. qp 20 is the closest
#: to parity that does not inflate the file by two thirds: 1.3 dB of SSIM behind
#: libx264 for 17 % more bytes, and side-by-side crops show softer film grain in the
#: dark areas and identical caption edges. Hardware encoders buy throughput with
#: bitrate; this is where that trade was set.
HW_QUALITY = 20

#: Where the frames cross into the GPU. **Last in the chain, always**: libass draws
#: on CPU frames, so a graph that uploaded before `subtitles=` would burn nothing.
VAAPI_UPLOAD_FILTER = "format=nv12,hwupload"

#: The render node VA-API opens. Conventional rather than discovered — a machine
#: whose node is elsewhere simply fails `encoder_opens` and `doctor` says so, which
#: is the honest outcome and costs nothing to be wrong about.
VAAPI_RENDER_NODE = "/dev/dri/renderD128"

#: The one-frame encode `encoder_opens` runs. Small and synthetic: the question is
#: whether a session opens at all, not how fast it is.
PROBE_SOURCE = "color=c=black:s=320x240:d=1"

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
    hw_encoder: str  # first *listed* of HW_ENCODER_PREFERENCE, else ""
    has_ffprobe: bool
    #: The first listed one that actually **opened** — the only one `--fast` may
    #: use. Kept apart from `hw_encoder` because the two genuinely differ: this
    #: machine lists `h264_qsv` and cannot open it (no MFX runtime), and a feature
    #: that trusted the listing would claim a hardware render while encoding on the
    #: CPU. Defaulted so the five-argument constructor in older tests still builds.
    fast_encoder: str = ""


@dataclass(frozen=True)
class VideoEncoder:
    """One H.264 encoder and everything a command line needs to drive it.

    Three parts, because the hardware encoders need all three and libx264 needs
    only the last: a device opened *before* the first input, a filter appended to
    the *end* of the video graph, and the codec flags themselves. The x264-only
    knobs (`-crf`, `-preset`, `-pix_fmt`) are absent rather than translated —
    VA-API rejects a pixel format it does not own, and its `-qp` is a quantiser,
    not a rate factor, so pretending they are the same number would be a lie in
    the argument list.
    """

    name: str
    quality_flag: str
    quality: int
    preset: str = ""
    pix_fmt: str = ""
    device_args: tuple[str, ...] = ()
    upload_filter: str = ""

    @property
    def is_hardware(self) -> bool:
        return self.name != SOFTWARE_ENCODER

    def input_args(self) -> list[str]:
        """What must be said before the first `-i`."""
        return list(self.device_args)

    def filter_chain(self, graph: str) -> str:
        """`graph` with the upload appended, or just the upload when there is no graph."""
        return ",".join(part for part in (graph, self.upload_filter) if part)

    def output_args(self) -> list[str]:
        """Codec, quality, and the knobs the encoder actually has.

        The order is M1's, flag for flag, so the software path's argument list is
        byte-identical to the one `tests/unit/test_audio_mix.py` pins.
        """
        args = ["-c:v", self.name]
        if self.preset:
            args += ["-preset", self.preset]
        args += [self.quality_flag, str(self.quality)]
        if self.pix_fmt:
            args += ["-pix_fmt", self.pix_fmt]
        return args

    def fingerprint(self) -> list[object]:
        """What a stage hash records. Name and quality decide the bytes; the device
        node and the upload filter are how they get there, and do not."""
        return [self.name, self.quality_flag, self.quality]


def software_encoder(*, crf: int, preset: str, pix_fmt: str) -> VideoEncoder:
    """libx264 exactly as M1 drove it."""
    return VideoEncoder(
        name=SOFTWARE_ENCODER, quality_flag="-crf", quality=crf, preset=preset, pix_fmt=pix_fmt
    )


def hardware_encoder(name: str, *, quality: int = HW_QUALITY) -> VideoEncoder:
    """One of `HW_ENCODER_PREFERENCE`, with the plumbing that particular one needs.

    Only VA-API needs plumbing: it encodes from GPU surfaces, so it wants a device
    and an upload. QSV opens its own session over software frames, and
    VideoToolbox takes them directly. `-q:v` there is documented rather than
    measured — nothing in this project runs on macOS.
    """
    if name == "h264_vaapi":
        return VideoEncoder(
            name=name,
            quality_flag="-qp",
            quality=quality,
            device_args=("-vaapi_device", VAAPI_RENDER_NODE),
            upload_filter=VAAPI_UPLOAD_FILTER,
        )
    if name == "h264_qsv":
        return VideoEncoder(name=name, quality_flag="-global_quality", quality=quality)
    return VideoEncoder(name=name, quality_flag="-q:v", quality=quality)


def _run(args: list[str]) -> str:
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    return result.stdout


def encoder_opens(name: str) -> bool:
    """Can this machine actually encode one frame with `name`?

    `ffmpeg -encoders` answers a different question — whether the *build* carries
    the wrapper — and on this machine the two disagree: `h264_qsv` is listed and
    dies with "Error creating a MFX session: -9" because no runtime is installed.
    Asking by encoding is the only answer worth reporting, and one 320x240 frame
    costs a fraction of a second against the minute of encoding it decides.
    """
    encoder = hardware_encoder(name)
    args = [
        "ffmpeg", "-hide_banner", "-nostdin", "-y",
        *encoder.input_args(),
        "-f", "lavfi", "-i", PROBE_SOURCE,
        "-frames:v", "1",
        "-vf", encoder.filter_chain("format=yuv420p"),
        *encoder.output_args(),
        "-f", "null", "-",
    ]
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    return result.returncode == 0


def probe_capabilities(*, verify: Callable[[str], bool] = encoder_opens) -> FFmpegCaps:
    """What this ffmpeg can do — including which hardware encoder it can *open*.

    `verify` is a seam so tests can answer that without a GPU; it defaults to the
    real thing, so `doctor` and the pipeline never report a listing as a capability.
    """
    if shutil.which("ffmpeg") is None:
        return FFmpegCaps(False, "", False, "", False)
    version_out = _run(["ffmpeg", "-hide_banner", "-version"])
    version = version_out.splitlines()[0].strip() if version_out else "unknown"
    filters = _run(["ffmpeg", "-hide_banner", "-filters"])
    encoders = _run(["ffmpeg", "-hide_banner", "-encoders"])
    listed = [name for name in HW_ENCODER_PREFERENCE if name in encoders]
    return FFmpegCaps(
        installed=True,
        version=version,
        has_subtitles_filter=" subtitles " in filters,
        hw_encoder=listed[0] if listed else "",
        has_ffprobe=shutil.which("ffprobe") is not None,
        fast_encoder=next((name for name in listed if verify(name)), ""),
    )


@lru_cache(maxsize=1)
def detect_fast_encoder() -> str:
    """The hardware encoder `--fast` would use, or `""`. Probed **once** per process.

    Cached because the probe shells out and every stage would otherwise re-ask it
    for every scene. `cache_clear()` is the seam a test needs.
    """
    return probe_capabilities().fast_encoder


def choose_encoder(
    *,
    fast: bool,
    crf: int,
    preset: str,
    pix_fmt: str,
    detect: Callable[[], str] = detect_fast_encoder,
) -> VideoEncoder:
    """The encoder this run should use. **libx264 unless fast mode finds hardware.**

    Falls back silently *here* and loudly at the call site: the caller compares
    `is_hardware` against what it asked for and raises a stage warning, because a
    `--fast` that quietly encodes on the CPU is the one outcome worse than not
    having the flag.
    """
    if fast:
        name = detect()
        if name:
            return hardware_encoder(name)
    return software_encoder(crf=crf, preset=preset, pix_fmt=pix_fmt)


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
    keep_stderr_lines: int = STDERR_TAIL_LINES,
) -> str:
    """Run ffmpeg with ``args``, raising :class:`FFmpegError` on failure.

    When ``on_progress`` is given, ffmpeg is asked for machine-readable progress
    and the callback receives the number of output seconds encoded so far.

    Returns the tail of stderr on success. Most callers ignore it; the two-pass
    loudness measurement reads its JSON block out of it, and raises
    ``keep_stderr_lines`` so a chattier ffmpeg cannot push that block off the end.
    """
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y"]
    if on_progress is not None:
        cmd += ["-progress", "pipe:1", "-nostats", "-stats_period", str(STATS_PERIOD_S)]
    cmd += args

    tail: deque[str] = deque(maxlen=max(keep_stderr_lines, STDERR_TAIL_LINES))
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

    stderr_tail = "\n".join(tail)
    if returncode != 0:
        raise FFmpegError(
            f"ffmpeg exited with status {returncode}:\n{stderr_tail}",
            returncode=returncode,
            stderr_tail=stderr_tail,
        )
    return stderr_tail


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


def extract_frame(video: str, *, at_s: float, dest: str, cwd: Path | None = None) -> None:
    """Write a single frame of ``video`` at ``at_s`` seconds to ``dest``.

    ``-ss`` before ``-i`` is the fast seek: ffmpeg jumps to the nearest keyframe
    before decoding, which for a still is the difference between milliseconds and
    decoding the whole timeline. ``-update 1`` tells the image muxer this is one
    picture and not the first of a numbered sequence, which it otherwise warns about.

    Paths are passed through untouched so the caller can keep the M0 relative-path
    rule (see :mod:`videomaker.pipeline.render`) by pairing them with ``cwd``.
    """
    run_ffmpeg(
        ["-ss", f"{at_s:.3f}", "-i", video, "-frames:v", "1", "-update", "1", dest],
        cwd=cwd,
    )
