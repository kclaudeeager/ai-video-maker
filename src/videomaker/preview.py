"""The 480p review preview — an artefact, not a pipeline stage.

The review gate needs something a browser can play in a couple of seconds, not the
full-resolution deliverable. So this re-runs `render`'s filter graph at 480 lines
with `-preset ultrafast`: the reviewer sees the real cuts, the real narration and
the real burned-in captions, just smaller and cheaper. It is throwaway output — the
thing that ships is still `output/final_wide.mp4`.

**It is deliberately outside `STAGE_ORDER`** (M2 design decision 4). Adding a stage
would move `derive_status`, `STATUS_AFTER`, the golden-path test and M1's proven
cache scoping, all to buy a review convenience. Instead it caches itself under its
own `preview:wide` key in the *same* `StageCache`, so it is skipped when nothing it
depends on changed and re-encoded when something did — without the runner, the CLI
or the status machine knowing it exists.

**The path rule from `render` applies unchanged.** FFmpeg parses the `subtitles=`
argument as a filter-graph token before it looks at the filesystem, so an absolute
`.ass` path breaks the parse the moment the project folder contains a colon (the M0
spike hit exactly this). Every path here is relative and FFmpeg runs with `cwd` set
to the project folder; the filter string itself is reused from `render` rather than
rebuilt, so the two can never drift apart.
"""

from collections.abc import Callable
from pathlib import Path

from videomaker.cache import hash_inputs, stage_key
from videomaker.media.ffmpeg import run_ffmpeg
from videomaker.models import Aspect, Project
from videomaker.pipeline.assemble import (
    AUDIO_RATE,
    BUILD_DIRNAME,
    SPECS,
    narration_relpath,
    video_relpath,
)
from videomaker.pipeline.base import StageDeps, content_hash, project_root
from videomaker.pipeline.captions import caption_path
from videomaker.pipeline.render import (
    AUDIO_CHANNELS,
    LOUDNORM,
    PIX_FMT,
    subtitles_filter,
)

#: The cache key's stage half. Intentionally absent from `cache.STAGE_ORDER`.
STAGE = "preview"

#: Lines of the preview. Width follows from the aspect: `scale=-2:480` keeps the
#: ratio and rounds to an even number, which h264 requires — 1920x1080 gives 854x480.
PREVIEW_HEIGHT = 480

#: Throwaway settings. `ultrafast` trades roughly a third more bitrate for several
#: times the speed, which is the right trade for a file nobody keeps.
PRESET = "ultrafast"
CRF = 28
AUDIO_BITRATE = "96k"

PREVIEW_ASPECT = Aspect.WIDE


def preview_relpath(aspect: Aspect) -> str:
    """Where the preview lives, relative to the project folder."""
    return f"{BUILD_DIRNAME}/preview_{aspect.value}.mp4"


def preview_scale_filter() -> str:
    """Downscale to `PREVIEW_HEIGHT`, letting FFmpeg pick the even width."""
    return f"scale=-2:{PREVIEW_HEIGHT}"


def preview_filter(root: Path, aspect: Aspect, *, burn_captions: bool) -> str:
    """The preview's video graph: scale first, then burn.

    Scaling before `subtitles=` means libass rasterises glyphs at 480 lines rather
    than at 1080 only to have them thrown away — the layout still lands correctly
    because the `.ass` file declares `PlayResX/PlayResY` and libass scales to the
    frame it is given.
    """
    scale = preview_scale_filter()
    return f"{scale},{subtitles_filter(root, aspect)}" if burn_captions else scale


def preview_hash(root: Path, aspect: Aspect, *, video: Path, narration: Path,
                 captions: Path | None) -> str:
    """Content of the inputs, plus every knob that shapes this encode."""
    return hash_inputs(
        video=content_hash(video),
        narration=content_hash(narration),
        captions=content_hash(captions) if captions is not None else None,
        filter=preview_filter(root, aspect, burn_captions=captions is not None),
        spec=[SPECS[aspect].fps, PREVIEW_HEIGHT],
        encoder=[PRESET, CRF, PIX_FMT, LOUDNORM, AUDIO_BITRATE, AUDIO_RATE, AUDIO_CHANNELS],
    )


def _preview_args(root: Path, aspect: Aspect, *, burn_captions: bool) -> list[str]:
    return [
        "-i",
        video_relpath(aspect),
        "-i",
        narration_relpath(aspect),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-vf",
        preview_filter(root, aspect, burn_captions=burn_captions),
        "-af",
        f"loudnorm={LOUDNORM}",
        "-c:v",
        "libx264",
        "-preset",
        PRESET,
        "-crf",
        str(CRF),
        "-pix_fmt",
        PIX_FMT,
        "-r",
        str(SPECS[aspect].fps),
        "-c:a",
        "aac",
        "-b:a",
        AUDIO_BITRATE,
        "-ar",
        str(AUDIO_RATE),
        "-ac",
        str(AUDIO_CHANNELS),
        "-shortest",
        "-movflags",
        "+faststart",
        preview_relpath(aspect),
    ]


def build_preview(
    project: Project,
    deps: StageDeps,
    *,
    on_progress: Callable[[float], None] | None = None,
) -> Path:
    """Return the project's 480p preview, encoding it only if it is out of date.

    `on_progress` receives output seconds encoded so far, exactly as `run_ffmpeg`
    reports them, so a caller can drive a progress bar.
    """
    aspect = PREVIEW_ASPECT
    root = project_root(deps, project)
    video = root / video_relpath(aspect)
    narration = root / narration_relpath(aspect)
    missing = [path.name for path in (video, narration) if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            f"cannot preview {project.id}: assemble has not produced {', '.join(missing)}"
        )

    captions = caption_path(deps, project, aspect)
    # Whether captions were burned is part of the hash, so a project that gains an
    # `.ass` file later re-previews rather than keeping a caption-less video.
    burn = captions.is_file()
    key = stage_key(STAGE, aspect.value)
    current = preview_hash(
        root, aspect, video=video, narration=narration, captions=captions if burn else None
    )
    out_path = root / preview_relpath(aspect)
    if not deps.stage_cache.is_stale(key, current) and out_path.is_file():
        return out_path

    out_path.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(
        _preview_args(root, aspect, burn_captions=burn), cwd=root, on_progress=on_progress
    )
    # Only the stage cache is persisted: the artefact is the file, and nothing on the
    # project itself changed — `project.outputs` names the deliverable, not this.
    deps.stage_cache.mark(key, current)
    deps.stage_cache.save()
    return out_path
