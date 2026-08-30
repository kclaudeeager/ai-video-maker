"""The 480p review preview, over **real** FFmpeg.

Mocking the encode here would test nothing worth testing: the whole point of the
artefact is that FFmpeg accepts the downscaled filter graph — including the
`subtitles=` argument, whose relative path is load-bearing (an absolute `.ass` path
breaks FFmpeg's filter-graph parser the moment a folder name contains a colon, the
M0 spike's finding). So the fixtures are a genuine 1080p silent video, a genuine
narration wav and a genuine `.ass` file; only their content is trivial.

The preview is deliberately **not** a `STAGE_ORDER` stage (design decision 4), so
these tests also pin the thing that would otherwise go unnoticed: it caches itself
under its own `preview:wide` key in the same `StageCache` without appearing in the
stage order the runner and `derive_status` walk.
"""

import json
import subprocess

import pytest

from videomaker.cache import STAGE_ORDER, ResponseCache, StageCache, stage_key
from videomaker.config import Settings
from videomaker.media.ass import write_ass
from videomaker.media.ffmpeg import probe_dimensions
from videomaker.models import Aspect, WordTiming
from videomaker.pipeline.assemble import WIDE_SPEC, narration_relpath, video_relpath
from videomaker.pipeline.base import StageDeps
from videomaker.pipeline.captions import PLAY_RES, STYLES, caption_relpath
from videomaker.preview import PREVIEW_HEIGHT, build_preview, preview_relpath
from videomaker.project import ProjectStore
from videomaker.providers.ratelimit import QuotaTracker

#: Short on purpose — the encodes are real, so every extra second is test wall time.
SOURCE_SECONDS = 2.0

WORDS = [
    WordTiming(word="Drives", start_s=0.1, end_s=0.7),
    WordTiming(word="store", start_s=0.7, end_s=1.2),
    WordTiming(word="bytes", start_s=1.2, end_s=1.8),
]


def _ffmpeg(args: list[str]) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-y", *args], check=True,
                   capture_output=True)


@pytest.fixture(scope="module")
def source_media(tmp_path_factory):
    """One real 1080p video and one real wav, encoded once for the whole module."""
    root = tmp_path_factory.mktemp("preview_source")
    video = root / "video.mp4"
    narration = root / "narration.wav"
    _ffmpeg([
        "-f", "lavfi",
        "-i", f"testsrc=size={WIDE_SPEC.width}x{WIDE_SPEC.height}:rate=30:d={SOURCE_SECONDS}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        str(video),
    ])
    # A tone, not `anullsrc`: `loudnorm` measures digital silence as -inf LUFS and
    # then hands the AAC encoder NaN. Real narration always has signal in it.
    _ffmpeg([
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", str(SOURCE_SECONDS), "-ac", "1", str(narration),
    ])
    return video, narration


def _write_captions(root, words):
    path = root / caption_relpath(Aspect.WIDE)
    path.parent.mkdir(parents=True, exist_ok=True)
    return write_ass(
        words, STYLES[Aspect.WIDE], path, play_res=PLAY_RES[Aspect.WIDE]
    )


@pytest.fixture
def assembled(tmp_path, source_media):
    """A project whose `build/` already holds what `assemble` would have left."""
    video, narration = source_media
    settings = Settings(workspace_dir=tmp_path / "workspace")
    deps = StageDeps(
        settings=settings,
        store=ProjectStore(settings.workspace_dir),
        stage_cache=StageCache(tmp_path / "stages.json"),
        response_cache=ResponseCache(tmp_path / "responses"),
        quota=QuotaTracker(tmp_path / "quota.json"),
    )
    project = deps.store.create("how ssds work", "tech_explainer", target_minutes=1.0)
    root = deps.store.path_for(project.id)
    (root / "build").mkdir(parents=True, exist_ok=True)
    (root / video_relpath(Aspect.WIDE)).write_bytes(video.read_bytes())
    (root / narration_relpath(Aspect.WIDE)).write_bytes(narration.read_bytes())
    _write_captions(root, WORDS)
    return project, deps, root


def test_preview_relpath_lives_in_build():
    assert preview_relpath(Aspect.WIDE) == "build/preview_wide.mp4"
    assert PREVIEW_HEIGHT == 480


def test_preview_is_480_high_at_the_wide_aspect_ratio(assembled):
    project, deps, root = assembled

    path = build_preview(project, deps)

    assert path == root / preview_relpath(Aspect.WIDE)
    width, height = probe_dimensions(path)
    assert height == PREVIEW_HEIGHT
    # h264 needs even dimensions, so 1920x1080 scaled to 480 high rounds up to 854.
    assert width == 854
    assert (width, height) != WIDE_SPEC.size


def test_a_second_call_with_nothing_changed_re_encodes_nothing(assembled):
    project, deps, _root = assembled

    first = build_preview(project, deps)
    stamp = first.stat().st_mtime_ns

    second = build_preview(project, deps)

    assert second == first
    assert second.stat().st_mtime_ns == stamp


def test_changing_the_captions_invalidates_the_preview(assembled):
    project, deps, root = assembled
    path = build_preview(project, deps)
    stamp = path.stat().st_mtime_ns

    _write_captions(root, [*WORDS, WordTiming(word="quickly", start_s=1.8, end_s=2.4)])

    assert build_preview(project, deps).stat().st_mtime_ns != stamp


def test_on_progress_receives_output_seconds(assembled):
    project, deps, _root = assembled
    ticks: list[float] = []

    build_preview(project, deps, on_progress=ticks.append)

    assert ticks
    assert all(tick >= 0 for tick in ticks)
    assert max(ticks) <= SOURCE_SECONDS + 1.0


def test_the_preview_is_cached_outside_the_stage_order(assembled):
    project, deps, _root = assembled

    build_preview(project, deps)

    key = stage_key("preview", Aspect.WIDE.value)
    assert key == "preview:wide"
    assert key in json.loads(deps.stage_cache.path.read_text())
    assert "preview" not in STAGE_ORDER
