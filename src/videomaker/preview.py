"""The 480p review preview — an artefact, not a pipeline stage.

The review gate needs something a browser can play in a couple of seconds, not the
full-resolution deliverable. So this re-runs `render`'s filter graph at 480 lines
with `-preset ultrafast`: the reviewer sees the real cuts, the real narration and
the real burned-in captions, just smaller and cheaper. It is throwaway output — the
thing that ships is still `output/final_wide.mp4`.

**It is deliberately outside `STAGE_ORDER`** (M2 design decision 4). Adding a stage
would move `derive_status`, `STATUS_AFTER`, the golden-path test and M1's proven
cache scoping, all to buy a review convenience. Instead it caches itself under its
own `preview:<aspect>` key in the *same* `StageCache`, so it is skipped when nothing
it depends on changed and re-encoded when something did — without the runner, the
CLI or the status machine knowing it exists.

**There are two proxies now, one per aspect** (M3 Task 11), because gate 3 shows the
long cut and the Short side by side and a reviewer who cannot see the Short cannot
judge it. They are two cache keys and two files, not one file cropped: `assemble`
has already authored `build/video_vertical.mp4` from the `in_short` subset, so this
only re-encodes it smaller. The vertical proxy **refuses** for a project whose Short
is empty or over three minutes — `render.check_short_limit` is the same rule the
deliverable is held to, and a preview of a cut that cannot ship would be a lie.
`short_problem` is the same refusal as a sentence, for a page that wants to say why
rather than raise.

**The preview carries the mix.** A music picker whose choice cannot be heard until
after the gate is stamped is a picker nobody would use, so the bed and the effects go
through this encode too, chosen by the very functions the real render uses
(`render.music_bed`, `render.sfx_plan`) so the two can never disagree about what
plays. One difference, deliberate: the deliverable measures its loudness in a first
pass and applies it in a second, and this throwaway file does not. A single pass is
what a no-music render does anyway, and a second decode of the whole timeline is a
poor price for a file that exists to be watched once.

Because the bed is not a file inside the project, mtime cannot tell a page whether
the proxy on disk was mixed with the track the project names today. So the knobs are
fingerprinted separately, under `preview-mix:<aspect>`, and `mix_is_current` answers
that question without hashing a hundred megabytes of video on every 1.5 s poll.

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
from videomaker.media.audio import (
    MusicBed,
    MusicMix,
    SfxPlan,
    mix_filter_complex,
    music_input_args,
)
from videomaker.media.ffmpeg import probe_duration, run_ffmpeg
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
    RENDER_ASPECTS,
    SHORT_ASPECT,
    ShortNotRenderable,
    check_short_limit,
    music_bed,
    scan_library,
    sfx_plan,
    subtitles_filter,
)

#: The cache key's stage half. Intentionally absent from `cache.STAGE_ORDER`.
STAGE = "preview"

#: The second key, and the reason it exists: the bed lives in the user's own
#: `assets/music/`, outside the project folder, so no mtime inside the project moves
#: when the choice does. Fingerprinting the mix's *knobs* (never a file's bytes) is
#: what lets a page poll "is this proxy still the mix the project asks for?" cheaply.
#: Absent from `cache.STAGE_ORDER` for the same reason `STAGE` is.
MIX_STAGE = "preview-mix"

#: Lines of the preview. Width follows from the aspect: `scale=-2:480` keeps the
#: ratio and rounds to an even number, which h264 requires — 1920x1080 gives 854x480.
PREVIEW_HEIGHT = 480

#: Throwaway settings. `ultrafast` trades roughly a third more bitrate for several
#: times the speed, which is the right trade for a file nobody keeps.
PRESET = "ultrafast"
CRF = 28
AUDIO_BITRATE = "96k"

PREVIEW_ASPECT = Aspect.WIDE

#: Both cuts, in the order gate 3 shows them — the same tuple `run_render` encodes,
#: so a proxy can never exist for an aspect the deliverable does not.
PREVIEW_ASPECTS: tuple[Aspect, ...] = RENDER_ASPECTS


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


def mix_fingerprint(bed: MusicBed | None, sfx: SfxPlan) -> str:
    """Everything about a mix that changes the finished bytes, minus the audio files.

    Knobs only — no `content_hash`, and **no `probe_duration`**, on purpose. This is
    read on every 1.5 s poll of gate 3 to answer "was the proxy on disk mixed with the
    track the project names now?", and neither digesting the library nor spawning an
    ffprobe per aspect is a fair price for that question. A track replaced under the
    same name is caught by the *encode* fingerprint (`preview_hash`), which does hash
    the bytes.
    """
    if bed is None and not sfx:
        return "silent"
    return hash_inputs(bed=bed.knobs() if bed is not None else None, sfx=sfx.knobs())


def preview_hash(
    root: Path,
    aspect: Aspect,
    *,
    video: Path,
    narration: Path,
    captions: Path | None,
    music: MusicBed | None = None,
    sfx: SfxPlan | None = None,
) -> str:
    """Content of the inputs, plus every knob that shapes this encode.

    Deliberately the same shape as `render.render_hash`, including the rule that
    matters: the mix is added **only when there is one**. `hash_inputs` covers the
    keys it is given, so an always-present key would move the fingerprint of every
    no-music project and re-encode every proxy in the workspace for a change nobody
    made. An empty `assets/music/` is every fresh clone.

    The mix's *length* is not here, and must not be: it is derived from the narration
    file, whose bytes are already hashed, and measuring it costs an ffprobe.
    """
    parts: dict[str, object] = {
        "video": content_hash(video),
        "narration": content_hash(narration),
        "captions": content_hash(captions) if captions is not None else None,
        "filter": preview_filter(root, aspect, burn_captions=captions is not None),
        "spec": [SPECS[aspect].fps, PREVIEW_HEIGHT],
        "encoder": [PRESET, CRF, PIX_FMT, LOUDNORM, AUDIO_BITRATE, AUDIO_RATE, AUDIO_CHANNELS],
    }
    if music is not None:
        parts["music"] = {**music.knobs(), "content": content_hash(music.path)}
    if sfx:
        parts["sfx"] = {
            "cues": sfx.knobs(),
            "content": [content_hash(path) for path in sfx.files],
        }
    return hash_inputs(**parts)


def _preview_args(
    root: Path, aspect: Aspect, *, burn_captions: bool, mix: MusicMix | None = None
) -> list[str]:
    args = [
        "-i",
        video_relpath(aspect),
        "-i",
        narration_relpath(aspect),
    ]
    if mix is None:
        args += ["-map", "0:v:0", "-map", "1:a:0"]
    else:
        # Video 0, narration 1, then the bed, then one input per distinct effect
        # file — the same order `render._render_args` uses, because `sfx_input`
        # below counts on it. The track's path appears only here, never in the
        # graph: M0 found FFmpeg parses a filter graph before it looks at the
        # filesystem, and the user's library lives outside the project folder so
        # it cannot be made relative.
        bed_args = music_input_args(mix.bed) if mix.bed is not None else []
        args += [*bed_args, *mix.sfx.input_args(), "-map", "0:v:0", "-map", "[aout]"]
    args += [
        "-vf",
        preview_filter(root, aspect, burn_captions=burn_captions),
    ]
    if mix is None:
        args += ["-af", f"loudnorm={LOUDNORM}"]
    else:
        args += [
            "-filter_complex",
            mix_filter_complex(
                mix.bed,
                duration_s=mix.duration_s,
                rate=AUDIO_RATE,
                channels=AUDIO_CHANNELS,
                target=LOUDNORM,
                speech=1,
                music=2,
                sfx=mix.sfx,
                sfx_input=2 + (1 if mix.bed is not None else 0),
                # No `measured`: one pass for a file that exists to be watched once.
                measured=None,
            ),
        ]
    args += [
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
    return args


def short_problem(project: Project, *, aspect: Aspect = SHORT_ASPECT) -> str:
    """Why this aspect has no preview, as a sentence, or `""` when it has one.

    `check_short_limit` raising is the right answer for a builder; it is the wrong
    answer for a page that has to render either way. This is the same refusal, in the
    same words, for a template to print beside an empty panel — so the reason a Short
    is missing is stated once and cannot drift between the CLI and the browser.
    """
    if aspect is not SHORT_ASPECT:
        return ""
    try:
        check_short_limit(project)
    except ShortNotRenderable as refusal:
        return str(refusal)
    return ""


def mix_parts(
    project: Project, deps: StageDeps, aspect: Aspect, *, library=None
) -> tuple[MusicBed | None, SfxPlan]:
    """The bed and this aspect's effects — everything the mix is, except its length.

    Kept apart from the length because measuring that costs an ffprobe and the page
    does not need it: `mix_fingerprint` answers "is the proxy still mixed as asked?"
    from the knobs alone, and it is asked once per aspect on every 1.5 s poll of
    gate 3. `build_preview` measures only when it is really about to encode.

    Both come from `render`'s own choosers, so the proxy and the deliverable can never
    disagree about which track plays or where a whoosh lands.
    """
    scanned = scan_library(deps) if library is None else library
    return (
        music_bed(project, deps, library=scanned),
        sfx_plan(project, deps, aspect, library=scanned),
    )


def mix_is_current(
    project: Project, deps: StageDeps, aspect: Aspect, *, library=None
) -> bool:
    """Was the proxy on disk mixed with what the project asks for now?

    Cheap on purpose — see `mix_fingerprint`. A project that has never been previewed
    reads as *not* current, which is the honest answer and costs one encode. Pass
    `library` when the caller has already scanned: gate 3 asks this once per aspect
    on every 1.5 s poll, and one walk of `assets/` for the page is plenty.
    """
    root = project_root(deps, project)
    if not (root / narration_relpath(aspect)).is_file():
        return False
    wanted = mix_fingerprint(*mix_parts(project, deps, aspect, library=library))
    return not deps.stage_cache.is_stale(stage_key(MIX_STAGE, aspect.value), wanted)


def build_preview(
    project: Project,
    deps: StageDeps,
    *,
    aspect: Aspect = PREVIEW_ASPECT,
    library=None,
    on_progress: Callable[[float], None] | None = None,
) -> Path:
    """Return one aspect's 480p preview, encoding it only if it is out of date.

    `on_progress` receives output seconds encoded so far, exactly as `run_ffmpeg`
    reports them, so a caller can drive a progress bar.

    Raises `ShortNotRenderable` for a vertical preview of a Short that could not
    ship — nothing marked `in_short`, or a cut over three minutes. That check comes
    **first**, before the missing-input check, so an empty Short is answered with the
    sentence that says how to fix it rather than with "assemble has not produced
    video_vertical.mp4", which is true and useless.
    """
    if aspect is SHORT_ASPECT:
        check_short_limit(project)

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
    bed, effects = mix_parts(project, deps, aspect, library=library)
    key = stage_key(STAGE, aspect.value)
    mix_key = stage_key(MIX_STAGE, aspect.value)
    current = preview_hash(
        root,
        aspect,
        video=video,
        narration=narration,
        captions=captions if burn else None,
        music=bed,
        sfx=effects,
    )
    fingerprint = mix_fingerprint(bed, effects)
    out_path = root / preview_relpath(aspect)
    if (
        not deps.stage_cache.is_stale(key, current)
        and not deps.stage_cache.is_stale(mix_key, fingerprint)
        and out_path.is_file()
    ):
        return out_path

    # Only now is the length worth an ffprobe: it shapes the `atrim` and the fade,
    # and nothing above it needed the number. The cut's length comes from the
    # narration bed, which `assemble` authored to the exact timeline.
    mix = (
        None
        if bed is None and not effects
        else MusicMix(
            bed=bed,
            duration_s=probe_duration(narration),
            measured=None,
            sfx=effects,
        )
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(
        _preview_args(root, aspect, burn_captions=burn, mix=mix),
        cwd=root,
        on_progress=on_progress,
    )
    # Only the stage cache is persisted: the artefact is the file, and nothing on the
    # project itself changed — `project.outputs` names the deliverable, not this.
    deps.stage_cache.mark(key, current)
    deps.stage_cache.mark(mix_key, fingerprint)
    deps.stage_cache.save()
    return out_path


def build_previews(
    project: Project,
    deps: StageDeps,
    *,
    aspects: tuple[Aspect, ...] = PREVIEW_ASPECTS,
    on_aspect: Callable[[Aspect], Callable[[float], None] | None] | None = None,
) -> dict[Aspect, Path | str]:
    """Every proxy gate 3 can show, keyed by aspect — a path, or the reason there is none.

    An aspect that refuses is **reported, not raised**: one unrenderable Short must
    not cost the reviewer the long cut that is sitting there finished. Anything else
    going wrong — a missing input, FFmpeg failing — still raises, because that is a
    fault rather than an editorial state.

    `on_aspect` hands back the progress hook for each aspect in turn, which is how a
    caller drives one bar across two encodes.
    """
    # One scan for the whole call, exactly as `run_render` takes one for a whole run:
    # a file dropped into `assets/music/` mid-encode must not make the two proxies
    # disagree about what is on disk.
    library = scan_library(deps)
    built: dict[Aspect, Path | str] = {}
    for aspect in aspects:
        refusal = short_problem(project, aspect=aspect)
        if refusal:
            built[aspect] = refusal
            continue
        built[aspect] = build_preview(
            project,
            deps,
            aspect=aspect,
            library=library,
            on_progress=on_aspect(aspect) if on_aspect is not None else None,
        )
    return built
