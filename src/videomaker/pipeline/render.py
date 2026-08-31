"""The render stage: one final pass that burns captions and mixes the narration.

Everything expensive has already happened — `assemble` left a concatenated silent
video, a narration bed and an `.ass` file — so this is a single encode: burn the
subtitles, mux the audio, normalise it to the streaming target of -14 LUFS, and move
the moov atom to the front so the file starts playing before it has finished loading.

**The path rule is load-bearing.** FFmpeg parses a filter argument before it ever
looks at the filesystem, so an absolute `.ass` path breaks the parse the moment the
project folder contains a colon (the M0 spike hit exactly this). Every path here is
relative and FFmpeg runs with `cwd` set to the project folder. The one exception is
the music track, which lives in the user's own library outside the project and so
cannot be made relative — it is an `-i` input, never a filter argument, and
`media/audio.music_input_args` is the only place it is written.

**With an empty `assets/music/` this stage does exactly what M1 shipped**, down to
the argument list and the stage hash, because that is the path every fresh clone
takes. Music adds a third input, an `amix` and a second loudness pass; nothing else.
"""

from pathlib import Path

from videomaker import audio as music_library
from videomaker.cache import hash_inputs, stage_key
from videomaker.media.audio import (
    MusicBed,
    MusicMix,
    measure_args,
    measure_loudness,
    mix_filter_complex,
    music_input_args,
)
from videomaker.media.ffmpeg import probe_duration, run_ffmpeg
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
    narration_relpath,
    scene_timeline,
    short_duration_s,
    short_fits,
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
) -> str:
    """Content of the three inputs, plus every knob that shapes the final encode.

    The music key is **added only when there is music**, rather than hashed as
    `None`. That is deliberate: `hash_inputs` covers the keys it is given, so an
    always-present key would change the fingerprint of every no-music project and
    re-encode every finished video in the workspace for no change in the output.

    This is also the whole of music's reach into the cache engine. Nothing upstream
    hashes `project.music`, so choosing a track re-renders and re-assembles nothing.
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
    return hash_inputs(**parts)


def _mood(project: Project) -> str:
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


def music_bed(project: Project, deps: StageDeps) -> MusicBed | None:
    """The track that plays under this project's renders, or `None` for narration only.

    Scanned with `probe=None`, so choosing a bed costs no ffprobe: the mix loops and
    trims without knowing a track's duration, and the index supplies one anyway when
    `videomaker music scan` has run. The index is read through the module rather than
    a direct import so tests can redirect it (`tests/conftest.py`).
    """
    selection = project.music
    if not selection.enabled:
        return None

    scanned = music_library.scan(
        deps.settings.music_dir,
        deps.settings.sfx_dir,
        index_path=music_library.DEFAULT_INDEX_PATH,
        probe=None,
    )
    track = music_library.select_track(
        scanned, mood=_mood(project), track_key=selection.track_key, seed=project.id
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
    root: Path, aspect: Aspect, *, burn_captions: bool, mix: MusicMix | None = None
) -> list[str]:
    spec = SPECS[aspect]
    args = [
        "-i",
        video_relpath(aspect),
        "-i",
        narration_relpath(aspect),
    ]
    if mix is None:
        args += ["-map", "0:v:0", "-map", "1:a:0"]
    else:
        args += [*music_input_args(mix.bed), "-map", "0:v:0", "-map", "[aout]"]
    if burn_captions:
        # A simple filtergraph for the picture even when the audio needs a complex
        # one: the two do not meet, and `-vf` keeps the M0 relative-path rule visible.
        args += ["-vf", subtitles_filter(root, aspect)]
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
                measured=mix.measured,
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


def _plan_mix(bed: MusicBed | None, root: Path, aspect: Aspect) -> MusicMix | None:
    """Place `bed` against this aspect's cut, measuring the mix's loudness first.

    The cut's length comes from the narration bed, which `assemble` authored to the
    exact timeline — that is what the `atrim` and the fade out are cut to. Pass one
    then measures the finished mix so pass two can hit -14 LUFS instead of landing
    near it; if it cannot be measured the render falls back to a single pass, which
    is what a no-music render does anyway.
    """
    if bed is None:
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
        ),
        cwd=root,
    )
    return MusicMix(bed=bed, duration_s=duration, measured=measured)


def run_render(project: Project, deps: StageDeps) -> StageResult:
    """Burn, mux and normalise, once per aspect: `render:wide`, `render:vertical`.

    The three-minute rule is checked *per aspect, inside the loop*, and the work
    already done is persisted in a `finally`. Both details matter: checking up front
    would let an over-long Short block the wide video too, and saving only on the
    happy path would throw away a wide render that had just succeeded, so the next
    run would re-encode it to reach the same refusal.
    """
    root = project_root(deps, project)
    bed = music_bed(project, deps)
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
            )
            out_path = root / output_relpath(aspect)
            if not deps.stage_cache.is_stale(key, current) and out_path.is_file():
                skipped += 1
                continue

            out_path.parent.mkdir(parents=True, exist_ok=True)
            run_ffmpeg(
                _render_args(root, aspect, burn_captions=burn, mix=_plan_mix(bed, root, aspect)),
                cwd=root,
            )
            spec = project.outputs.get(aspect)
            if spec is not None:
                spec.video_path = output_relpath(aspect)
            deps.stage_cache.mark(key, current)
            changed = True
    finally:
        if changed:
            deps.stage_cache.save()
            deps.store.save(project)
    return StageResult(changed=changed, skipped_units=skipped)
