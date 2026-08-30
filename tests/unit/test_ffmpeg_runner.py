import pytest

from videomaker.media.ffmpeg import (
    FFmpegError,
    probe_dimensions,
    probe_duration,
    probe_json,
    run_ffmpeg,
)


def _make_clip(path, *, size="64x48", duration=1):
    run_ffmpeg(
        [
            "-f", "lavfi",
            "-i", f"color=c=black:s={size}:r=10:d={duration}",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-pix_fmt", "yuv420p",
            str(path),
        ]
    )
    return path


def test_run_ffmpeg_success_writes_output(tmp_path):
    out = _make_clip(tmp_path / "ok.mp4")
    assert out.exists() and out.stat().st_size > 0


def test_run_ffmpeg_raises_with_stderr_tail_on_failure(tmp_path):
    with pytest.raises(FFmpegError) as excinfo:
        run_ffmpeg(["-i", str(tmp_path / "does-not-exist.mp4"), str(tmp_path / "out.mp4")])
    message = str(excinfo.value)
    assert "does-not-exist.mp4" in message
    assert "No such file" in message


def test_run_ffmpeg_reports_increasing_progress(tmp_path):
    seen: list[float] = []
    run_ffmpeg(
        [
            "-f", "lavfi",
            "-i", "testsrc=s=320x240:r=30:d=30",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-f", "null", "-",
        ],
        on_progress=seen.append,
    )
    assert len(seen) >= 2
    assert seen == sorted(seen)
    assert seen[-1] > seen[0] >= 0


def test_run_ffmpeg_honours_cwd(tmp_path):
    run_ffmpeg(
        ["-f", "lavfi", "-i", "color=c=red:s=64x48:r=10:d=1", "-c:v", "libx264", "rel.mp4"],
        cwd=tmp_path,
    )
    assert (tmp_path / "rel.mp4").exists()


def test_probe_json_reports_streams_and_format(tmp_path):
    info = probe_json(_make_clip(tmp_path / "probe.mp4"))
    assert "streams" in info and "format" in info
    assert any(stream["codec_type"] == "video" for stream in info["streams"])


def test_probe_duration_and_dimensions(tmp_path):
    clip = _make_clip(tmp_path / "dims.mp4", size="128x72", duration=2)
    assert probe_duration(clip) == pytest.approx(2.0, abs=0.2)
    assert probe_dimensions(clip) == (128, 72)


def test_probe_json_raises_ffmpeg_error_for_missing_file(tmp_path):
    with pytest.raises(FFmpegError):
        probe_json(tmp_path / "nope.mp4")
