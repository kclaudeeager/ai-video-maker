"""The captions stage: every scene's words on one project-wide timeline.

`align` times each scene's words from zero **within that scene**, because each scene
is voiced separately (M0 finding 7). The final video plays those scenes back to back,
so the words must be shifted by the running offset before they are written out:
scene two's first word belongs at `scene one's duration + SCENE_GAP_S`, not at 0.0.
Concatenating without that offset produces captions that look perfect on scene one
and drift further out of sync with every scene after it.

The offset uses the **measured** `duration_s` (plus the gap), never the last word's
end, because that is exactly the length `assemble` gives each scene's video segment.
The unit is the aspect, not the scene: one `.ass` file covers the whole video, so any
scene's words changing rewrites it.

The running offset is also **per aspect**. Vertical plays only the `in_short` subset
(spec 4.5), so its offsets are the sum of the *preceding in_short* scenes and their
gaps — never the wide running total. Both are built by walking `assemble.aspect_scenes`
for the aspect in hand, which is the same list `assemble.scene_timeline` walks, so
caption group *n* and video segment *n* start at the same second by construction.
"""

from dataclasses import asdict
from pathlib import Path

from videomaker.cache import hash_inputs, stage_key
from videomaker.media.ass import STYLES, write_ass
from videomaker.models import Aspect, Project, WordTiming
from videomaker.pipeline.assemble import SPECS, aspect_scenes
from videomaker.pipeline.base import SCENE_GAP_S, StageDeps, StageResult, project_root

STAGE = "captions"
CAPTIONS_DIRNAME = "captions"

#: Which aspects this stage actually writes an `.ass` file for. Vertical joins it
#: once its style is authored (Task 3) — never by scaling the wide layout.
CAPTION_ASPECTS: tuple[Aspect, ...] = (Aspect.WIDE,)

#: Timeline resolution the ASS coordinates are authored against, taken from the frame
#: `assemble` actually encodes. Authoring captions against a different resolution than
#: the video would leave libass silently rescaling the layout they were designed for.
PLAY_RES: dict[Aspect, tuple[int, int]] = {aspect: spec.size for aspect, spec in SPECS.items()}


def caption_relpath(aspect: Aspect) -> str:
    """Where the burned-in subtitle file lives, relative to the project folder.

    Relative because `render` passes it to FFmpeg's `subtitles=` filter, whose
    argument is parsed as a filter-graph token: absolute paths with colons in them
    need escaping that relative paths under `cwd` avoid entirely.
    """
    return f"{CAPTIONS_DIRNAME}/{aspect.value}.ass"


def caption_path(deps: StageDeps, project: Project, aspect: Aspect) -> Path:
    return project_root(deps, project) / caption_relpath(aspect)


def timeline_word_groups(
    project: Project, aspect: Aspect = Aspect.WIDE
) -> list[list[WordTiming]]:
    """Each scene's words shifted onto **this aspect's** timeline, kept split per scene.

    The scene list is `aspect_scenes`, never `project.scenes`. That is the whole
    correctness argument for the Short: vertical plays only the `in_short` subset, so
    a scene dropped from the middle of the long cut must not reserve any time in the
    vertical timeline. Offsetting by the wide running total instead would leave the
    Short's first scene looking perfect and put every caption after the dropped scene
    late by that scene's duration plus a gap — the same "measured against the wrong
    timeline" fault M2's render progress bar hit.

    Scenes with no measured duration are skipped outright rather than counted as
    zero-length: `assemble` cannot build a segment for a scene it has no audio for,
    so reserving time for one would shift every later caption off its narration. That
    is exactly `assemble.is_assemblable`, so group *n* starts where segment *n* does —
    asserted directly in `tests/unit/test_assemble_vertical.py`.
    """
    groups: list[list[WordTiming]] = []
    offset = 0.0
    for scene in aspect_scenes(project, aspect):
        if scene.duration_s is None:
            continue
        groups.append(
            [
                word.model_copy(
                    update={"start_s": word.start_s + offset, "end_s": word.end_s + offset}
                )
                for word in scene.words
            ]
        )
        # Exactly the segment length `assemble` builds for this scene. The two must
        # agree: see `SCENE_GAP_S`.
        offset += scene.duration_s + SCENE_GAP_S
    return groups


def timeline_words(project: Project, aspect: Aspect = Aspect.WIDE) -> list[WordTiming]:
    """`timeline_word_groups` flattened: this aspect's words, in playback order.

    Derived from the groups rather than re-walking the scenes, so the two can never
    disagree about an offset — one of them being fixed and the other not is precisely
    how a caption drift survives a green test suite.
    """
    return [word for group in timeline_word_groups(project, aspect) for word in group]


def aspect_hash(project: Project, aspect: Aspect) -> str:
    """This aspect's caption inputs: its own scenes, its own style, its own frame.

    The scene list is the aspect's cut, so re-marking a scene `in_short` restages the
    vertical captions and leaves the wide ones alone. The style is looked up rather
    than indexed: an aspect whose layout has not been authored yet hashes as `None`
    instead of raising, which is what lets a vertical unit be *listed* before the
    vertical style exists. Wide is unaffected either way — its hash must stay
    byte-identical to M1's or every rendered project restages.
    """
    style = STYLES.get(aspect)
    return hash_inputs(
        scenes=[
            {
                "id": scene.id,
                # Durations decide the offsets, so they are inputs as much as the words.
                "duration_s": scene.duration_s,
                "words": [word.model_dump() for word in scene.words],
            }
            for scene in aspect_scenes(project, aspect)
        ],
        style=asdict(style) if style is not None else None,
        aspect=aspect.value,
        play_res=list(PLAY_RES[aspect]),
        gap_s=SCENE_GAP_S,
    )


def run_captions(project: Project, deps: StageDeps) -> StageResult:
    """Write one `.ass` file per aspect. One unit per aspect: `captions:wide`."""
    changed = False
    skipped = 0

    for aspect in CAPTION_ASPECTS:
        key = stage_key(STAGE, aspect.value)
        current = aspect_hash(project, aspect)
        path = caption_path(deps, project, aspect)
        words = timeline_words(project, aspect)
        if not words:
            # Nothing aligned yet: the runner aligns before it captions, so this is a
            # no-op, never an error — and an empty file would only mislead `render`.
            skipped += 1
            continue
        if not deps.stage_cache.is_stale(key, current) and path.is_file():
            skipped += 1
            continue

        write_ass(
            words,
            STYLES[aspect],
            path,
            play_res=PLAY_RES[aspect],
            groups=timeline_word_groups(project, aspect),
        )
        deps.stage_cache.mark(key, current)
        changed = True

    if changed:
        # Nothing on the project itself changed — the artefact is the file — so only
        # the stage cache is persisted.
        deps.stage_cache.save()
    return StageResult(changed=changed, skipped_units=skipped)
