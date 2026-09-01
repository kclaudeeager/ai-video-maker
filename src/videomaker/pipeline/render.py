"""The render stage: one final pass that burns captions and mixes the narration.

Everything expensive has already happened — `assemble` left a concatenated silent
video, a narration bed and an `.ass` file — so this is a single encode: burn the
subtitles, mux the audio, normalise it to the streaming target of -14 LUFS, and move
the moov atom to the front so the file starts playing before it has finished loading.

**The path rule is load-bearing.** FFmpeg parses a filter argument before it ever
looks at the filesystem, so an absolute `.ass` path breaks the parse the moment the
project folder contains a colon (the M0 spike hit exactly this). Every path here is
relative and FFmpeg runs with `cwd` set to the project folder. The one exception is
the user's own audio — the music track and the sound effects — which lives outside
the project and so cannot be made relative. Those are `-i` inputs, never filter
arguments, and `media/audio.music_input_args` and `SfxPlan.input_args` are the only
places their paths are written.

**With an empty `assets/music/` and `assets/sfx/` this stage does exactly what M1
shipped**, down to the argument list and the stage hash, because that is the path
every fresh clone takes — the owner's included. Music adds an input, an `amix` and a
second loudness pass. Effects add one input per distinct file and one `adelay` per
placement; where they land is decided by `sfx_plan`, out of the cut timestamps
`scene_timeline` already knows and the beats `Scene.beat` already records.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from videomaker import audio as music_library
from videomaker.cache import hash_inputs, stage_key
from videomaker.config import Settings
from videomaker.media.audio import (
    DEFAULT_SFX_PROFILE,
    SFX_PROFILES,
    MusicBed,
    MusicMix,
    SfxPlan,
    SfxProfile,
    measure_args,
    measure_loudness,
    mix_filter_complex,
    music_input_args,
    plan_sfx,
)
from videomaker.media.ffmpeg import VideoEncoder, probe_duration, run_ffmpeg
from videomaker.models import Aspect, Project
from videomaker.pipeline.assemble import (
    ASSEMBLE_ASPECTS,
    AUDIO_RATE,
    CRF,
    MAX_SHORT_S,
    PIX_FMT,
    PRESET,
    SPECS,
    Segment,
    default_encoder,
    narration_relpath,
    scene_timeline,
    short_duration_s,
    short_fits,
    stage_encoder,
    video_relpath,
)
from videomaker.pipeline.base import SCENE_GAP_S, StageDeps, StageResult, content_hash, project_root
from videomaker.pipeline.captions import caption_path, caption_relpath
from videomaker.templates import load_template

STAGE = "render"
OUTPUT_DIRNAME = "output"

#: Where M3 will drop the bundled OFL faces. It does not exist yet, so `fontsdir` is
#: omitted rather than pointed at nothing: libass would fall back to fontconfig anyway,
#: and a `fontsdir` naming an absent directory is a lie the next reader has to check.
FONTS_RELDIR = "assets/fonts"

#: Streaming loudness target (-14 LUFS, -1.5 dBTP). Applied in **one** pass when
#: there is no music — that is the render M1 shipped, and a fresh clone must not
#: re-encode every finished video because this task landed. With a bed under the
#: narration it is applied in two: M1 measured a single pass landing at -14.9 LUFS,
#: and a mix makes that ~1 LU miss more audible (M1 follow-up 11).
LOUDNORM = "I=-14:TP=-1.5:LRA=11"

AUDIO_BITRATE = "192k"
#: Stereo out even from mono narration: some players quietly refuse mono AAC.
AUDIO_CHANNELS = 2

RENDER_ASPECTS: tuple[Aspect, ...] = ASSEMBLE_ASPECTS

#: The aspect the three-minute rule applies to. Named rather than written inline so
#: the rule reads as "the Short is capped", not "9:16 videos are capped": the cap is
#: a platform limit on Shorts, and nothing about 1080x1920 requires it.
SHORT_ASPECT = Aspect.VERTICAL


class ShortNotRenderable(RuntimeError):
    """The `in_short` subset cannot be published as a Short.

    Raised rather than worked around, because both ways of "fixing" it silently are
    worse than stopping: truncating an over-long cut at exactly `MAX_SHORT_S` ends
    the Short mid-sentence, and rendering an empty one writes a zero-length
    `final_vertical.mp4` that looks like a successful run. The user's lever is the
    `in_short` tick, so the message names the scenes worth unticking.
    """


def output_relpath(aspect: Aspect) -> str:
    return f"{OUTPUT_DIRNAME}/final_{aspect.value}.mp4"


def scenes_to_untick(project: Project) -> list[Segment]:
    """The fewest `in_short` scenes whose removal brings the Short under the cap.

    Longest first, so the advice is the shortest list of ticks to clear; ties break
    on scene id so two runs never suggest different scenes for the same project.
    Returns `[]` when the Short already fits — there is nothing to advise.

    Advice only. Nothing here writes to the project: choosing what a Short leaves out
    is an editorial decision, and the whole reason `in_short` is a per-scene flag
    rather than a duration cap is that the tool must not make it (spec 4.5).
    """
    segments = scene_timeline(project, gap_s=SCENE_GAP_S, aspect=SHORT_ASPECT)
    remaining = sum(segment.duration_s for segment in segments)
    picked: list[Segment] = []
    for segment in sorted(segments, key=lambda seg: (-seg.duration_s, seg.scene_id)):
        if remaining <= MAX_SHORT_S:
            break
        picked.append(segment)
        remaining -= segment.duration_s
    return picked


def check_short_limit(project: Project) -> None:
    """Raise unless the `in_short` subset is publishable as a Short.

    Two failures, deliberately kept apart from `assemble.short_fits`, which is a
    *duration* test and answers `True` for a project with nothing ticked at all —
    0 s fits. This is the *publishing* test, and an empty Short fails it.
    """
    segments = scene_timeline(project, gap_s=SCENE_GAP_S, aspect=SHORT_ASPECT)
    if not segments:
        raise ShortNotRenderable(
            "nothing is marked for the Short: tick “in short” on at least one "
            f"voiced scene before rendering {output_relpath(SHORT_ASPECT)}"
        )
    if short_fits(project):
        return

    duration = short_duration_s(project)
    named = ", ".join(
        f"{segment.scene_id} ({segment.duration_s:.1f}s)" for segment in scenes_to_untick(project)
    )
    raise ShortNotRenderable(
        f"the Short runs {duration:.1f}s, over the {MAX_SHORT_S:.0f}s limit for "
        f"{output_relpath(SHORT_ASPECT)}: untick “in short” on {named}, or on any "
        f"scenes totalling {duration - MAX_SHORT_S:.1f}s, and render again. "
        "Nothing is truncated — a Short cut off at the limit ends mid-sentence."
    )


def fonts_reldir(root: Path) -> str:
    """`assets/fonts` if this project has one, else no fonts directory at all."""
    return FONTS_RELDIR if (root / FONTS_RELDIR).is_dir() else ""


def subtitles_filter(root: Path, aspect: Aspect) -> str:
    """The `subtitles=` filter, always with a project-relative `.ass` path."""
    graph = f"subtitles={caption_relpath(aspect)}"
    fonts = fonts_reldir(root)
    return f"{graph}:fontsdir={fonts}" if fonts else graph


def render_hash(
    root: Path,
    aspect: Aspect,
    *,
    video: Path,
    narration: Path,
    captions: Path | None,
    music: MusicBed | None = None,
    sfx: SfxPlan | None = None,
    encoder: VideoEncoder | None = None,
) -> str:
    """Content of the three inputs, plus every knob that shapes the final encode.

    The music key is **added only when there is music**, rather than hashed as
    `None`. That is deliberate: `hash_inputs` covers the keys it is given, so an
    always-present key would change the fingerprint of every no-music project and
    re-encode every finished video in the workspace for no change in the output.
    The effects follow the same rule for the same reason, and an empty
    `assets/sfx/` is the state of every fresh clone.

    **The hardware encoder is written under that same rule**, and the choice is
    worth stating: `--fast` is a *how* rather than a *what*, which argues for
    leaving it out — but a VA-API encode at qp 20 is measurably not a libx264
    encode at CRF 18 (SSIM 0.9934 against 0.9952, and a fifth more bytes), and a
    cache that called them the same would answer `--fast` on a finished project by
    doing nothing at all. So it is hashed, and hashed *conditionally*, so that the
    default path's fingerprint is the one M1 shipped and the ten projects in the
    owner's workspace stay exactly where they are. The cost is deliberate and paid
    twice: the first `--fast` run re-encodes, and so does the first run after it.

    This is also the whole of the audio layer's reach into the cache engine. Nothing
    upstream hashes `project.music` or the library, so choosing a track or dropping in
    a whoosh re-renders and re-assembles nothing.
    """
    spec = SPECS[aspect]
    parts: dict[str, object] = {
        "video": content_hash(video),
        "narration": content_hash(narration),
        "captions": content_hash(captions) if captions is not None else None,
        "subtitles": subtitles_filter(root, aspect),
        "spec": [spec.width, spec.height, spec.fps],
        "encoder": [PRESET, CRF, PIX_FMT, LOUDNORM, AUDIO_BITRATE, AUDIO_RATE, AUDIO_CHANNELS],
    }
    if music is not None:
        parts["music"] = {**music.knobs(), "content": content_hash(music.path)}
    if sfx:
        parts["sfx"] = {
            "cues": sfx.knobs(),
            "content": [content_hash(path) for path in sfx.files],
        }
    if encoder is not None and encoder.is_hardware:
        parts["hw_encoder"] = encoder.fingerprint()
    return hash_inputs(**parts)


def music_mood_for(project: Project) -> str:
    """The template's `music_mood`, or none at all if the template cannot be read.

    A template that has been renamed or edited into invalidity must not stop a render
    that is otherwise ready: the mood only decides *which* bed plays.
    """
    if project.music.mood:
        return project.music.mood
    try:
        return load_template(project.template).music_mood
    except ValueError:
        return ""


def sfx_profile_for(project: Project) -> SfxProfile:
    """How loud this project's effects sit, per the template's `sfx_profile`.

    Falls back rather than raising, for `music_mood_for`'s reason: a template renamed
    out from under a finished project must not stop a render that is otherwise ready.
    The profile only decides how loud a whoosh is.
    """
    try:
        name = load_template(project.template).sfx_profile
    except ValueError:
        name = DEFAULT_SFX_PROFILE
    return SFX_PROFILES.get(name, SFX_PROFILES[DEFAULT_SFX_PROFILE])


def scan_library(deps: StageDeps) -> music_library.Library:
    """The user's library as it is on disk right now.

    Scanned with `probe=None`, so the whole audio layer costs no ffprobe: the mix
    loops and trims without knowing a track's duration, and effects are placed by the
    timeline rather than by their own length (`media/audio.ROLE_LEAD_S` says why).
    The index is read through the module rather than a direct import so tests can
    redirect it (`tests/conftest.py`).
    """
    return music_library.scan(
        deps.settings.music_dir,
        deps.settings.sfx_dir,
        index_path=music_library.DEFAULT_INDEX_PATH,
        probe=None,
    )


def sfx_wanted(project: Project, settings: Settings) -> bool:
    """Whether this project plays effects at all.

    `MusicSelection.sfx_enabled` is an override in the same sense as its levels:
    `None` means "no opinion", so `config.yaml` decides, and gate 3's toggle can
    silence one video without changing the default for every other one.
    """
    override = project.music.sfx_enabled
    return settings.sfx_enabled if override is None else override


def sfx_plan(
    project: Project,
    deps: StageDeps,
    aspect: Aspect,
    *,
    library: music_library.Library | None = None,
) -> SfxPlan:
    """Where this aspect's effects land — **an empty `assets/sfx/` places nothing.**

    That is the default on every fresh clone, the owner's included, so it is the path
    this function takes most often and it must cost nothing: no placements, no extra
    inputs, no change to the render fingerprint, no re-encode.

    Everything it needs it already has. `scene_timeline` knows every cut timestamp for
    *this* aspect — which is why the Short's whooshes land on the Short's own cuts and
    not on the wide video's — and `Scene.beat` records which of the template's
    `structure` beats each scene was written for. Nothing here calls a model or looks
    at the footage.
    """
    if not sfx_wanted(project, deps.settings):
        return SfxPlan()
    scanned = scan_library(deps) if library is None else library
    segments = scene_timeline(project, gap_s=SCENE_GAP_S, aspect=aspect)
    return plan_sfx(
        [(segment.scene_id, segment.start_s) for segment in segments],
        beats={scene.id: scene.beat for scene in project.scenes},
        pick=lambda role: music_library.select_sfx(scanned, role, seed=project.id),
        profile=sfx_profile_for(project),
        transitions=deps.settings.transition_sfx_enabled,
    )


def music_bed(
    project: Project, deps: StageDeps, *, library: music_library.Library | None = None
) -> MusicBed | None:
    """The track that plays under this project's renders, or `None` for narration only."""
    selection = project.music
    if not selection.enabled:
        return None

    scanned = scan_library(deps) if library is None else library
    track = music_library.select_track(
        scanned,
        mood=music_mood_for(project),
        track_key=selection.track_key,
        seed=project.id,
    )
    if track is None:
        return None

    volume = selection.volume_db
    duck = selection.duck_db
    return MusicBed(
        path=track.path.resolve(),
        key=track.key,
        volume_db=deps.settings.music_volume_db if volume is None else volume,
        duck_db=deps.settings.duck_amount_db if duck is None else duck,
    )


def _render_args(
    root: Path,
    aspect: Aspect,
    *,
    burn_captions: bool,
    mix: MusicMix | None = None,
    encoder: VideoEncoder | None = None,
) -> list[str]:
    spec = SPECS[aspect]
    encoder = encoder or default_encoder()
    args = [
        # A hardware device is opened before ffmpeg reads a frame; libx264 adds
        # nothing here, so the default path's argument list is untouched.
        *encoder.input_args(),
        "-i",
        video_relpath(aspect),
        "-i",
        narration_relpath(aspect),
    ]
    if mix is None:
        args += ["-map", "0:v:0", "-map", "1:a:0"]
    else:
        # Video 0, narration 1, then the bed if there is one, then one input per
        # distinct effect file. `sfx_input` below has to agree with this order.
        bed_args = music_input_args(mix.bed) if mix.bed is not None else []
        args += [*bed_args, *mix.sfx.input_args(), "-map", "0:v:0", "-map", "[aout]"]
    # A simple filtergraph for the picture even when the audio needs a complex one:
    # the two do not meet, and `-vf` keeps the M0 relative-path rule visible. The
    # hardware upload goes on the **end** — libass draws on CPU frames, so uploading
    # first would hand the encoder a picture with no captions on it — and a hardware
    # render therefore needs a `-vf` even when there are no captions to burn.
    video_graph = encoder.filter_chain(subtitles_filter(root, aspect) if burn_captions else "")
    if video_graph:
        args += ["-vf", video_graph]
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
                measured=mix.measured,
            ),
        ]
    args += [
        *encoder.output_args(),
        "-r",
        str(spec.fps),
        "-c:a",
        "aac",
        "-b:a",
        AUDIO_BITRATE,
        "-ar",
        str(AUDIO_RATE),
        "-ac",
        str(AUDIO_CHANNELS),
        # The narration bed is authored to the exact timeline; stop with the shorter
        # stream rather than freezing on a tail of padding.
        "-shortest",
        "-movflags",
        "+faststart",
        output_relpath(aspect),
    ]
    return args


def _plan_mix(
    bed: MusicBed | None, root: Path, aspect: Aspect, *, sfx: SfxPlan | None = None
) -> MusicMix | None:
    """Place `bed` and the effects against this aspect's cut, measuring first.

    `None` when there is neither, which is the fresh clone: that is what keeps the
    argument list, the graph and the stage hash exactly M1's, with one `-af` and a
    single loudness pass.

    The cut's length comes from the narration bed, which `assemble` authored to the
    exact timeline — that is what the `atrim` and the fade out are cut to. Pass one
    then measures the finished mix, effects included, so pass two can hit -14 LUFS
    instead of landing near it; if it cannot be measured the render falls back to a
    single pass, which is what a no-music render does anyway.
    """
    sfx = sfx or SfxPlan()
    if bed is None and not sfx:
        return None
    duration = probe_duration(root / narration_relpath(aspect))
    measured = measure_loudness(
        measure_args(
            bed,
            narration=narration_relpath(aspect),
            duration_s=duration,
            rate=AUDIO_RATE,
            channels=AUDIO_CHANNELS,
            target=LOUDNORM,
            sfx=sfx,
        ),
        cwd=root,
    )
    return MusicMix(bed=bed, duration_s=duration, measured=measured, sfx=sfx)


@dataclass
class _AspectProgress:
    """One aspect's encode, reported on the **stage's** clock rather than its own.

    FFmpeg reports the output seconds of the file in front of it, from zero, and
    this stage encodes one file per aspect. A caller handed those readings raw would
    watch the count rewind when the Short starts, which on a progress bar reads as a
    crash. So each aspect is offset by what the aspects before it reached, and
    `reached` is what the next one is offset by.

    This lives here, and not in the web layer that wants it, because *this* is the
    code that knows there is more than one file. M2 did the same arithmetic outside,
    against a substituted `run_ffmpeg`, and could only get away with it while one
    worker thread ran one job at a time.
    """

    on_progress: Callable[[float], None]
    offset: float
    reached: float = 0.0

    def __call__(self, seconds: float) -> None:
        self.reached = max(self.reached, seconds)
        self.on_progress(self.offset + seconds)


def run_render(
    project: Project,
    deps: StageDeps,
    *,
    on_progress: Callable[[float], None] | None = None,
) -> StageResult:
    """Burn, mux and normalise, once per aspect: `render:wide`, `render:vertical`.

    The three-minute rule is checked *per aspect, inside the loop*, and the work
    already done is persisted in a `finally`. Both details matter: checking up front
    would let an over-long Short block the wide video too, and saving only on the
    happy path would throw away a wide render that had just succeeded, so the next
    run would re-encode it to reach the same refusal.

    `on_progress` receives output seconds **for the whole stage**, climbing once
    across every aspect encoded — see `_AspectProgress`. It is `None` on the CLI
    path, and that is not merely a default: asking FFmpeg for `-progress pipe:1`
    costs a pipe and a reading loop that nothing would consume.
    """
    root = project_root(deps, project)
    encoder, warnings = stage_encoder(deps.settings)
    encoded = 0.0  # output seconds finished by the aspects already rendered
    # One scan for the whole run: the bed and every aspect's effects come out of the
    # same reading of the library, so a file dropped in mid-render cannot make the
    # wide video and the Short disagree about what is on disk.
    library = scan_library(deps)
    bed = music_bed(project, deps, library=library)
    changed = False
    skipped = 0

    try:
        for aspect in RENDER_ASPECTS:
            if aspect is SHORT_ASPECT:
                # Before the `is_file` check below, so an over-long or empty Short is
                # an error rather than a silent skip when nothing was assembled.
                check_short_limit(project)

            video = root / video_relpath(aspect)
            narration = root / narration_relpath(aspect)
            if not (video.is_file() and narration.is_file()):
                # Nothing assembled yet: the runner assembles first, so this is a no-op.
                skipped += 1
                continue

            effects = sfx_plan(project, deps, aspect, library=library)
            captions = caption_path(deps, project, aspect)
            # Whether captions were burned is part of the hash, so a project that gains
            # an `.ass` file later re-renders rather than keeping a caption-less video.
            burn = captions.is_file()
            key = stage_key(STAGE, aspect.value)
            current = render_hash(
                root,
                aspect,
                video=video,
                narration=narration,
                captions=captions if burn else None,
                music=bed,
                sfx=effects,
                encoder=encoder,
            )
            out_path = root / output_relpath(aspect)
            if not deps.stage_cache.is_stale(key, current) and out_path.is_file():
                skipped += 1
                continue

            out_path.parent.mkdir(parents=True, exist_ok=True)
            # A skipped aspect encodes nothing, so it banks nothing: the tracker is
            # built here, past the cache check, rather than at the top of the loop.
            tracker = None if on_progress is None else _AspectProgress(on_progress, encoded)
            run_ffmpeg(
                _render_args(
                    root,
                    aspect,
                    burn_captions=burn,
                    mix=_plan_mix(bed, root, aspect, sfx=effects),
                    encoder=encoder,
                ),
                cwd=root,
                on_progress=tracker,
            )
            if tracker is not None:
                encoded += tracker.reached
            spec = project.outputs.get(aspect)
            if spec is not None:
                spec.video_path = output_relpath(aspect)
            deps.stage_cache.mark(key, current)
            changed = True
    finally:
        if changed:
            deps.stage_cache.save()
            deps.store.save(project)
    return StageResult(changed=changed, skipped_units=skipped, warnings=warnings)
