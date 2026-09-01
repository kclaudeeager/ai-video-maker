"""The assemble stage: one uniform silent segment per scene, concatenated.

The stage is split in two on purpose. `build_scene_filter` is a **pure** function
returning the filter graph as a string, so the operations that decide what the frame
looks like — the pre-scale, the pan, the trim, the loop — are unit-testable without
running FFmpeg at all. `run_assemble` is the part that touches disk: it encodes each
scene's graph to a uniform intermediate (same codec, size, pixel format and frame
rate for every scene), then joins them with the **concat demuxer and stream copy**, so
the join itself costs no quality and almost no time.

Two things here must agree with other modules or the video desynchronises:

* Each segment lasts `scene.duration_s + SCENE_GAP_S`, exactly the offset `captions`
  advances its timeline by. `SCENE_GAP_S` is imported, never re-declared.
* `scene_timeline` skips exactly what `captions.timeline_words` skips — a scene with
  no measured duration — so segment *n* starts where caption *n* starts.

Motion is a treatment for **stills** only; footage already moves. The default,
`Motion.PAN`, is an animated crop over a 120 % pre-scale: smoother and far cheaper
than `zoompan`. `Motion.ZOOM` is opt-in Ken Burns and pre-upscales 2×, because the M0
spike showed visible jitter when `zoompan` computes its window on a 1× frame. Nothing
ever crops before it scales — cropping first throws away the pixels the scale needs.

The one exception is `reframe_filter`, which cuts an aspect's own window out of the
source before anything else runs. That is what makes vertical a re-crop of the asset
rather than a crop of the finished wide render (a global constraint of M3), and it is
safe precisely because the pixels it discards are ones no frame of that aspect could
show. Wide gets no reframe, so its graphs are byte-identical to M1's.
"""

import json
import math
from dataclasses import dataclass
from pathlib import Path

from videomaker.cache import hash_inputs, stage_key
from videomaker.config import Settings
from videomaker.media.ffmpeg import (
    VideoEncoder,
    choose_encoder,
    detect_fast_encoder,
    run_ffmpeg,
    software_encoder,
)
from videomaker.models import Aspect, Motion, OutputSpec, Project, Scene
from videomaker.pipeline.base import (
    SCENE_GAP_S,
    StageDeps,
    StageResult,
    content_hash,
    project_root,
)

STAGE = "assemble"
BUILD_DIRNAME = "build"

#: Encoder settings for the intermediates. Every segment shares them, which is what
#: makes the concat demuxer able to stream-copy rather than re-encode.
FPS = 30
CRF = 18
PRESET = "veryfast"
PIX_FMT = "yuv420p"
#: Fixed so every segment's mp4 shares one timescale; concat copy needs them uniform.
TIMESCALE = 30000

#: The narration bed `render` muxes: one canonical format, so the per-scene wavs
#: (Kokoro's 24 kHz mono, M0 finding 4) never reach the final mux unconverted.
AUDIO_RATE = 48000

#: Pre-scale factors. Pan needs only enough slack to travel across; zoom needs a 2×
#: grid for `zoompan` to compute sub-pixel windows on without jittering (M0 spike).
PRESCALE_PAN = 1.2
PRESCALE_ZOOM = 2.0
#: How much of the available horizontal slack a pan crosses, and how far zoom pushes in.
PAN_TRAVEL = 1.0
ZOOM_RATE = 0.15

#: Frames the `loop` filter may buffer. It holds decoded frames in memory (~3 MB each
#: at 1080p), so an uncapped loop over a long clip would cost gigabytes; past this we
#: repeat a shorter window of the same clip instead.
MAX_LOOP_FRAMES = 120

#: Which aspects this stage actually *encodes*. Both, now that `reframe_filter` can
#: cut a 9:16 window out of each scene's own source: vertical is never a scaled copy
#: of the wide render, and its cut is the `in_short` subset, not the whole project.
ASSEMBLE_ASPECTS: tuple[Aspect, ...] = (Aspect.WIDE, Aspect.VERTICAL)

#: **The platform limit — the only one anything refuses on.** The longest a Short
#: may run: YouTube Shorts, Reels and TikTok all accept three minutes and no more,
#: so `in_short` has to give way. `render.check_short_limit` raises on it.
MAX_SHORT_S = 180.0

#: **The editorial target — advice, never a refusal.** Where engagement actually
#: peaks in 2026: 30-45 s on YouTube Shorts, 21-34 s on TikTok, under 30 s on
#: Reels (which does not recommend anything over three minutes to new audiences at
#: all). A cut between this and `MAX_SHORT_S` is legal everywhere and competitive
#: nowhere, which is why the storyboard nudges about it — and why nothing in the
#: pipeline, the CLI or the web gates ever blocks on it. Trimming to a clock would
#: end a Short mid-sentence; the lever is the per-scene `in_short` tick.
SHORT_TARGET_S = 45.0

#: What a run says when `--fast` was asked for and there is nothing to be fast with.
#: A stage warning rather than a failure — the render is still correct, only as slow
#: as it always was — but never silence: a `--fast` that encodes on the CPU while
#: reporting nothing is the one outcome worse than not shipping the flag.
NO_HARDWARE_WARNING = (
    "fast render was asked for, but no hardware encoder on this machine could be "
    "opened; encoding with libx264 (CPU). Run `videomaker doctor` for the reason."
)


def stage_encoder(settings: Settings) -> tuple[VideoEncoder, tuple[str, ...]]:
    """The encoder both encoding stages use, and anything the run should be told.

    One definition for `assemble` and `render` together, because they must agree:
    the concat demuxer stream-copies the segments into the final input, and a build
    holding both libx264 and VA-API segments makes FFmpeg rewrite the timestamps as
    it joins them ("Non-monotonic DTS", measured). The fingerprints are what stop
    that happening across runs; this is what stops it happening within one.
    """
    encoder = choose_encoder(
        fast=settings.render_fast_mode,
        crf=CRF,
        preset=PRESET,
        pix_fmt=PIX_FMT,
        detect=detect_fast_encoder,
    )
    if settings.render_fast_mode and not encoder.is_hardware:
        return encoder, (NO_HARDWARE_WARNING,)
    return encoder, ()


def default_encoder() -> VideoEncoder:
    """libx264 with M1's knobs — what every argument builder falls back to."""
    return software_encoder(crf=CRF, preset=PRESET, pix_fmt=PIX_FMT)


@dataclass(frozen=True)
class VideoSpec:
    """The frame every stage of one aspect authors against.

    Shared rather than re-declared: `captions` writes its ASS coordinates in this
    resolution, and burned-in text authored for a different frame would be scaled by
    libass and stop matching the layout it was designed for.
    """

    aspect: Aspect
    width: int
    height: int
    fps: int = FPS

    @property
    def size(self) -> tuple[int, int]:
        return (self.width, self.height)


SPECS: dict[Aspect, VideoSpec] = {
    Aspect.WIDE: VideoSpec(aspect=Aspect.WIDE, width=1920, height=1080),
    Aspect.VERTICAL: VideoSpec(aspect=Aspect.VERTICAL, width=1080, height=1920),
}
WIDE_SPEC = SPECS[Aspect.WIDE]
VERTICAL_SPEC = SPECS[Aspect.VERTICAL]

#: Every aspect the tool authors a frame for. The per-stage tuples above and in
#: `captions`/`render` are the subsets each stage can *produce*; they have now grown
#: to meet this one, so a listed unit and a required unit are the same thing again.
#: They stay separate because that is what let vertical be listed while it was being
#: built, and it is what a third aspect would need in turn.
ALL_ASPECTS: tuple[Aspect, ...] = tuple(SPECS)


@dataclass(frozen=True)
class Segment:
    """One scene's slot on the finished timeline. `duration_s` includes the gap."""

    scene_id: str
    start_s: float
    duration_s: float


# ------------------------------------------------------------------- paths


def segment_relpath(scene_id: str, aspect: Aspect) -> str:
    return f"{BUILD_DIRNAME}/{scene_id}_{aspect.value}.mp4"


def concat_relpath(aspect: Aspect) -> str:
    return f"{BUILD_DIRNAME}/concat_{aspect.value}.txt"


def timeline_relpath(aspect: Aspect) -> str:
    return f"{BUILD_DIRNAME}/timeline_{aspect.value}.json"


def video_relpath(aspect: Aspect) -> str:
    return f"{BUILD_DIRNAME}/video_{aspect.value}.mp4"


def narration_relpath(aspect: Aspect) -> str:
    return f"{BUILD_DIRNAME}/narration_{aspect.value}.wav"


# ---------------------------------------------------------------- the timeline


def segment_duration(scene: Scene, *, gap_s: float) -> float:
    """How long this scene occupies the finished video: narration plus the gap."""
    if scene.duration_s is None:
        raise ValueError(f"scene {scene.id} has no measured duration; voice it first")
    return scene.duration_s + gap_s


def segment_frames(scene: Scene, spec: VideoSpec, *, gap_s: float) -> int:
    """Whole frames in this scene's segment.

    Rounded to nearest rather than up: a segment can only be a whole number of
    frames, and always rounding up would push every later cut systematically late.
    """
    return max(round(segment_duration(scene, gap_s=gap_s) * spec.fps), 1)


def is_assemblable(scene: Scene) -> bool:
    """Same test `captions.timeline_words` applies: has this scene been voiced?"""
    return scene.duration_s is not None


def aspect_scenes(project: Project, aspect: Aspect) -> list[Scene]:
    """The scenes that belong to this aspect's cut, in the project's own order.

    Wide is the whole project. Vertical is the `in_short` subset — the Short is a
    shorter *edit*, not a re-frame of the long video, which is why `in_short` is a
    per-scene flag and not a duration cap applied at the end.

    Unvoiced scenes are still included here: this is the membership test, and
    `scene_timeline` applies `is_assemblable` on top of it. Keeping the two apart is
    what lets the wide fingerprints stay byte-identical to M1, which hashed every
    scene whether or not it had been voiced.
    """
    if aspect is Aspect.WIDE:
        return list(project.scenes)
    return [scene for scene in project.scenes if scene.in_short]


def scene_timeline(
    project: Project, *, gap_s: float, aspect: Aspect = Aspect.WIDE
) -> list[Segment]:
    """Where each scene sits on this aspect's finished video.

    The starts must equal the offsets `captions.timeline_words` applies to its words;
    `tests/unit/test_stage_assemble.py` asserts the two agree.
    """
    segments: list[Segment] = []
    offset = 0.0
    for scene in aspect_scenes(project, aspect):
        if not is_assemblable(scene):
            continue
        duration = segment_duration(scene, gap_s=gap_s)
        segments.append(Segment(scene_id=scene.id, start_s=offset, duration_s=duration))
        offset += duration
    return segments


def timeline_scene_ids(project: Project, aspect: Aspect) -> list[str]:
    """The scenes this aspect's video is built from, in order."""
    return [
        segment.scene_id for segment in scene_timeline(project, gap_s=SCENE_GAP_S, aspect=aspect)
    ]


def timeline_duration_s(project: Project, aspect: Aspect) -> float:
    """How long this aspect's cut runs, gaps included."""
    return sum(
        segment.duration_s
        for segment in scene_timeline(project, gap_s=SCENE_GAP_S, aspect=aspect)
    )


def short_duration_s(project: Project) -> float:
    """The running time of the `in_short` subset."""
    return timeline_duration_s(project, Aspect.VERTICAL)


def short_fits(project: Project) -> bool:
    """Whether the `in_short` subset is short enough to publish as a Short.

    Measured against `MAX_SHORT_S`, the **limit**. A duration test only: a project
    with nothing marked `in_short` runs for 0 s and so "fits" vacuously. Callers
    that need a Short to *exist* check the timeline is non-empty; conflating the two
    here would hide an empty cut behind a green tick.
    """
    return short_duration_s(project) <= MAX_SHORT_S


def short_meets_target(project: Project) -> bool:
    """Whether the `in_short` subset is as tight as `SHORT_TARGET_S` wants.

    The **advisory** counterpart of `short_fits`. Nothing gates on this — it exists
    so the storyboard can say "this is longer than the Shorts sweet spot" while the
    Short stays perfectly renderable. Read it, report it, never raise on it.
    """
    return short_duration_s(project) <= SHORT_TARGET_S


# ------------------------------------------------------------- the filter graph


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return min(max(value, low), high)


def _is_video_asset(scene: Scene) -> bool:
    chosen = scene.visual.chosen
    return chosen is not None and chosen.duration_s is not None


def _pan_travel(scene: Scene) -> tuple[float, float]:
    """Start and end of the pan as fractions of the crop's horizontal slack.

    Centred on `crop_focus_x` and clamped to the frame, so a subject hard against one
    edge is never panned off it. Direction alternates with the scene number: every
    shot drifting the same way reads as a slideshow.
    """
    focus = _clamp(scene.visual.crop_focus_x)
    half = PAN_TRAVEL / 2
    start, end = _clamp(focus - half), _clamp(focus + half)
    digits = scene.id.lstrip("s")
    if digits.isdigit() and int(digits) % 2:
        start, end = end, start
    return start, end


def reframe_filter(spec: VideoSpec, focus: float) -> list[str]:
    """Cut the spec's own window out of the *source*, at `focus`.

    This is what makes vertical a genuine re-frame rather than a crop of the finished
    wide render: the pixels come from the asset's own file, so a Short never inherits
    the wide cut's burned-in captions or its 16:9 composition (spec 4.5).

    `min(iw, ih*w/h)` keeps the window inside the source, so an asset already at or
    narrower than the target ratio passes through untouched instead of being cropped
    to nothing. `focus` slides it: 0.0 is the left edge, 1.0 the right, 0.5 centred.

    Cropping before scaling is normally forbidden here — it throws away the pixels the
    scale needs. This crop is the exception, and only because it discards pixels no
    frame of this aspect could ever show; everything the later scale consumes is
    inside the window. Landscape specs get nothing at all: `_cover_scale` already
    frames a 16:9 output, and an empty list keeps M1's wide graphs byte-identical.

    The expression is quoted because it contains a comma, which the filter-graph
    parser would otherwise read as the end of the whole filter.
    """
    if spec.width >= spec.height:
        return []
    divisor = math.gcd(spec.width, spec.height)
    ratio = f"{spec.width // divisor}/{spec.height // divisor}"
    return [f"crop='min(iw,ih*{ratio})':ih:'(iw-ow)*{_clamp(focus):.3f}':0"]


def _cover_scale(width: int, height: int) -> str:
    """Scale to cover `width`×`height`, keeping the source's aspect ratio.

    `increase` never letterboxes: the excess is what the crop then spends on motion
    (and, for a 1024² Flux square, what M0 finding 5 warns is being discarded).
    """
    return f"scale={width}:{height}:force_original_aspect_ratio=increase"


def _motion_filters(scene: Scene, spec: VideoSpec, duration: float) -> list[str]:
    """Scale-then-crop for the scene's motion. Never crop before scaling."""
    motion = scene.visual.motion if not _is_video_asset(scene) else Motion.NONE
    focus = _clamp(scene.visual.crop_focus_x)

    if motion is Motion.ZOOM:
        # `zoompan` computes its window on the pre-scaled frame, so a 2x grid is what
        # keeps consecutive windows from snapping between whole source pixels.
        frames = max(round(duration * spec.fps), 1)
        return [
            _cover_scale(round(spec.width * PRESCALE_ZOOM), round(spec.height * PRESCALE_ZOOM)),
            (
                f"zoompan=z='1+{ZOOM_RATE:.3f}*on/{frames}'"
                ":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                f":d=1:s={spec.width}x{spec.height}:fps={spec.fps}"
            ),
        ]

    if motion is Motion.PAN:
        start, end = _pan_travel(scene)
        # The travel is clamped in Python rather than with `clip()` in the expression:
        # an expression containing commas would have to be escaped as a filter argument.
        return [
            _cover_scale(round(spec.width * PRESCALE_PAN), round(spec.height * PRESCALE_PAN)),
            (
                f"crop={spec.width}:{spec.height}"
                f":x='(iw-ow)*({start:.3f}+({end:.3f}-{start:.3f})*t/{duration:.3f})'"
                ":y=(ih-oh)/2"
            ),
        ]

    return [
        _cover_scale(spec.width, spec.height),
        f"crop={spec.width}:{spec.height}:x=(iw-ow)*{focus:.3f}:y=(ih-oh)/2",
    ]


def _source_filters(scene: Scene, spec: VideoSpec, duration: float) -> list[str]:
    """Turn a video asset into exactly `duration` seconds, trimming and looping."""
    chosen = scene.visual.chosen
    assert chosen is not None and chosen.duration_s is not None
    trim_start = max(scene.visual.trim_start_s, 0.0)
    available = max(chosen.duration_s - trim_start, 0.0)

    window = duration if available >= duration else min(available, MAX_LOOP_FRAMES / spec.fps)
    window = max(window, 1.0 / spec.fps)
    filters = [
        f"trim=start={trim_start:.3f}:duration={window:.3f}",
        "setpts=PTS-STARTPTS",
        f"fps={spec.fps}",
    ]
    if window < duration:
        # `loop` repeats the frames it has buffered, so the window is bounded above:
        # see MAX_LOOP_FRAMES. `loop=N` means N *further* passes over the buffer.
        frames = max(round(window * spec.fps), 1)
        repeats = math.ceil(duration / window) - 1
        filters += [
            f"loop=loop={repeats}:size={frames}:start=0",
            # `loop` re-emits buffered timestamps; renumber so the trim below sees a
            # monotonic constant-rate stream.
            f"setpts=N/{spec.fps}/TB",
        ]
    return filters


def build_scene_filter(scene: Scene, spec: VideoSpec, *, gap_s: float) -> str:
    """The filter graph turning this scene's asset into its segment.

    Pure: it reads the scene and returns a string. Everything the finished frame
    depends on is visible in that string, which is what makes a wrong segment
    diagnosable without decoding a video.
    """
    duration = segment_duration(scene, gap_s=gap_s)
    if scene.visual.chosen is None or not scene.visual.chosen.local_path:
        raise ValueError(f"scene {scene.id} has no chosen asset; run the visuals stage first")

    filters: list[str] = []
    if _is_video_asset(scene):
        filters += _source_filters(scene, spec, duration)
    filters += reframe_filter(spec, scene.visual.crop_focus_x)
    filters += _motion_filters(scene, spec, duration)
    filters += [
        f"fps={spec.fps}",
        f"trim=duration={duration:.3f}",
        "setpts=PTS-STARTPTS",
        "setsar=1",
        f"format={PIX_FMT}",
    ]
    return ",".join(filters)


# --------------------------------------------------------------------- the stage


def scene_hash(
    scene: Scene,
    spec: VideoSpec,
    graph: str,
    asset_digest: str,
    *,
    encoder: VideoEncoder | None = None,
) -> str:
    """Everything that decides the bytes of one segment.

    The filter graph is hashed as a string, so motion, focus, trim and the gap are all
    covered without listing them one by one; the asset is hashed by *content*, so a
    re-downloaded but different clip re-encodes while an identical one does not.

    The hardware key is **written only when there is hardware**, exactly as
    `render.render_hash` writes the music key only when there is music: `hash_inputs`
    covers the keys it is given, so an always-present key would change the
    fingerprint of every segment in the workspace and re-encode a finished build for
    a `--fast` nobody asked for. Written when it is there, because a VA-API segment
    is genuinely not an x264 segment — see `stage_encoder` for what happens when the
    two meet in one concat list.
    """
    parts: dict[str, object] = {
        "graph": graph,
        "asset": asset_digest,
        "frames": segment_frames(scene, spec, gap_s=SCENE_GAP_S),
        "spec": [spec.width, spec.height, spec.fps],
        "encoder": [PRESET, CRF, PIX_FMT, TIMESCALE],
    }
    if encoder is not None and encoder.is_hardware:
        parts["hw_encoder"] = encoder.fingerprint()
    return hash_inputs(**parts)


def _segment_args(
    scene: Scene,
    spec: VideoSpec,
    graph: str,
    out_relpath: str,
    *,
    encoder: VideoEncoder | None = None,
) -> list[str]:
    """The command line for one scene's uniform silent intermediate.

    Pure, like `build_scene_filter` and for the same reason: an intermediate encoded
    with the wrong codec still plays, so the only test that can name the fault is one
    that reads the argument list.
    """
    chosen = scene.visual.chosen
    assert chosen is not None
    encoder = encoder or default_encoder()
    duration = segment_duration(scene, gap_s=SCENE_GAP_S)
    frames = segment_frames(scene, spec, gap_s=SCENE_GAP_S)

    args: list[str] = encoder.input_args()
    if not _is_video_asset(scene):
        # A still is a one-frame input; the demuxer has to be told to keep serving it.
        args += ["-loop", "1", "-framerate", str(spec.fps), "-t", f"{duration:.3f}"]
    args += ["-i", chosen.local_path, "-vf", encoder.filter_chain(graph)]
    args += [
        "-an",
        "-sn",
        # Exactly this many frames, so segments never disagree with the timeline by
        # the fraction of a frame their durations round to.
        "-frames:v",
        str(frames),
        *encoder.output_args(),
        "-r",
        str(spec.fps),
        "-video_track_timescale",
        str(TIMESCALE),
        out_relpath,
    ]
    return args


def _encode_segment(
    root: Path,
    scene: Scene,
    spec: VideoSpec,
    graph: str,
    out_relpath: str,
    *,
    encoder: VideoEncoder | None = None,
) -> None:
    """Encode one scene's uniform silent intermediate.

    Run from the project folder with relative paths throughout — the M0 spike found
    absolute paths break FFmpeg's filter-graph parser as soon as one contains a colon.
    """
    (root / out_relpath).parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(_segment_args(scene, spec, graph, out_relpath, encoder=encoder), cwd=root)


def _write_concat_list(root: Path, aspect: Aspect, segments: list[Segment]) -> None:
    """The concat demuxer's playlist. Paths are relative to the list file's folder."""
    lines = [
        f"file '{Path(segment_relpath(segment.scene_id, aspect)).name}'" for segment in segments
    ]
    path = root / concat_relpath(aspect)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


def _write_timeline(root: Path, spec: VideoSpec, segments: list[Segment]) -> None:
    """Where every scene starts, in the finished video's own seconds.

    Written for debugging and for M2's storyboard: when captions look out of sync,
    this file and the `.ass` timestamps are the two numbers to compare.
    """
    payload = {
        "aspect": spec.aspect.value,
        "width": spec.width,
        "height": spec.height,
        "fps": spec.fps,
        "gap_s": SCENE_GAP_S,
        "total_duration_s": round(sum(segment.duration_s for segment in segments), 3),
        "scenes": [
            {
                "id": segment.scene_id,
                "start_s": round(segment.start_s, 3),
                "duration_s": round(segment.duration_s, 3),
                "path": segment_relpath(segment.scene_id, spec.aspect),
            }
            for segment in segments
        ],
    }
    path = root / timeline_relpath(spec.aspect)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))


def _concat_segments(root: Path, aspect: Aspect) -> None:
    """Join the intermediates. Stream copy: the segments are already uniform."""
    run_ffmpeg(
        [
            "-f",
            "concat",
            "-i",
            concat_relpath(aspect),
            "-c",
            "copy",
            video_relpath(aspect),
        ],
        cwd=root,
    )


def _build_narration(
    root: Path, project: Project, aspect: Aspect, segments: list[Segment]
) -> None:
    """One narration bed: each scene's wav padded to its segment, then concatenated.

    Padded rather than cut: the gap is silence after the words, which is exactly the
    room `captions` leaves before the next scene's first caption.

    The bed is built from `segments`, so each aspect gets its own: vertical is a
    **re-concatenation of the very same per-scene wavs** over the `in_short` subset
    (spec 4.5), never a re-synthesis. Nothing here reaches for a TTS provider, and
    nothing here may — a Short that re-voiced its scenes would cost a second run of
    the most expensive stage and, worse, drift from the takes the long cut used.
    `tests/unit/test_assemble_vertical.py` asserts the zero TTS calls directly.
    """
    args: list[str] = []
    chains: list[str] = []
    for index, segment in enumerate(segments):
        scene = project.scene_by_id(segment.scene_id)
        assert scene.audio_path is not None
        args += ["-i", scene.audio_path]
        chains.append(
            f"[{index}:a]aresample={AUDIO_RATE},aformat=sample_fmts=s16:channel_layouts=mono,"
            f"apad=whole_dur={segment.duration_s:.3f},"
            f"atrim=end={segment.duration_s:.3f},asetpts=N/SR/TB[a{index}]"
        )
    joined = "".join(f"[a{index}]" for index in range(len(segments)))
    chains.append(f"{joined}concat=n={len(segments)}:v=0:a=1[out]")
    args += [
        "-filter_complex",
        ";".join(chains),
        "-map",
        "[out]",
        "-c:a",
        "pcm_s16le",
        "-ar",
        str(AUDIO_RATE),
        "-ac",
        "1",
        narration_relpath(aspect),
    ]
    run_ffmpeg(args, cwd=root)


def _update_output_spec(project: Project, spec: VideoSpec, segments: list[Segment]) -> bool:
    """Record the aspect's frame and scene order; `render` fills in `video_path`."""
    existing = project.outputs.get(spec.aspect)
    updated = OutputSpec(
        aspect=spec.aspect,
        width=spec.width,
        height=spec.height,
        scene_ids=[segment.scene_id for segment in segments],
        video_path=existing.video_path if existing else None,
    )
    if existing == updated:
        return False
    project.outputs[spec.aspect] = updated
    return True


def run_assemble(project: Project, deps: StageDeps) -> StageResult:
    """Encode every stale scene's segment, then concatenate.

    Units are `assemble:<aspect>:<scene id>` for the segments and `assemble:<aspect>`
    for the join, so editing one scene re-encodes one segment and re-runs a stream
    copy — the "<10 s re-run" promise depends on nothing else being touched.
    """
    root = project_root(deps, project)
    encoder, warnings = stage_encoder(deps.settings)
    changed = False
    skipped = 0

    for aspect in ASSEMBLE_ASPECTS:
        spec = SPECS[aspect]
        segments = scene_timeline(project, gap_s=SCENE_GAP_S, aspect=aspect)
        if not segments:
            # Nothing voiced yet: the runner voices before it assembles, so this is a
            # no-op rather than an error.
            skipped += 1
            continue

        digests: list[str] = []
        for segment in segments:
            scene = project.scene_by_id(segment.scene_id)
            graph = build_scene_filter(scene, spec, gap_s=SCENE_GAP_S)
            asset = scene.visual.chosen
            assert asset is not None
            current = scene_hash(
                scene, spec, graph, content_hash(root / asset.local_path), encoder=encoder
            )
            digests.append(current)

            key = stage_key(STAGE, f"{aspect.value}:{scene.id}")
            relpath = segment_relpath(scene.id, aspect)
            if not deps.stage_cache.is_stale(key, current) and (root / relpath).is_file():
                skipped += 1
                continue

            _encode_segment(root, scene, spec, graph, relpath, encoder=encoder)
            deps.stage_cache.mark(key, current)
            changed = True

        key = stage_key(STAGE, aspect.value)
        current = hash_inputs(
            segments=digests,
            starts=[round(segment.start_s, 3) for segment in segments],
            narration=[
                content_hash(root / project.scene_by_id(segment.scene_id).audio_path)
                for segment in segments
                if project.scene_by_id(segment.scene_id).audio_path
            ],
            audio_rate=AUDIO_RATE,
        )
        outputs = [video_relpath(aspect), narration_relpath(aspect), timeline_relpath(aspect)]
        if not deps.stage_cache.is_stale(key, current) and all(
            (root / relpath).is_file() for relpath in outputs
        ):
            skipped += 1
        else:
            _write_concat_list(root, aspect, segments)
            _write_timeline(root, spec, segments)
            _concat_segments(root, aspect)
            _build_narration(root, project, aspect, segments)
            deps.stage_cache.mark(key, current)
            changed = True

        changed |= _update_output_spec(project, spec, segments)

    if changed:
        deps.stage_cache.save()
        deps.store.save(project)
    return StageResult(changed=changed, skipped_units=skipped, warnings=warnings)
