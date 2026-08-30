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
"""

from dataclasses import asdict
from pathlib import Path

from videomaker.cache import hash_inputs, stage_key
from videomaker.media.ass import STYLES, write_ass
from videomaker.models import Aspect, Project, WordTiming
from videomaker.pipeline.base import SCENE_GAP_S, StageDeps, StageResult, project_root

STAGE = "captions"
CAPTIONS_DIRNAME = "captions"

#: M1 renders wide only; nothing here assumes a single aspect (M3 adds vertical).
CAPTION_ASPECTS: tuple[Aspect, ...] = (Aspect.WIDE,)

#: Timeline resolution the ASS coordinates are authored against, per aspect.
PLAY_RES: dict[Aspect, tuple[int, int]] = {
    Aspect.WIDE: (1920, 1080),
}


def caption_relpath(aspect: Aspect) -> str:
    """Where the burned-in subtitle file lives, relative to the project folder.

    Relative because `render` passes it to FFmpeg's `subtitles=` filter, whose
    argument is parsed as a filter-graph token: absolute paths with colons in them
    need escaping that relative paths under `cwd` avoid entirely.
    """
    return f"{CAPTIONS_DIRNAME}/{aspect.value}.ass"


def caption_path(deps: StageDeps, project: Project, aspect: Aspect) -> Path:
    return project_root(deps, project) / caption_relpath(aspect)


def timeline_words(project: Project) -> list[WordTiming]:
    """Every scene's words, shifted onto the finished video's timeline.

    Scenes with no measured duration are skipped outright rather than counted as
    zero-length: `assemble` cannot build a segment for a scene it has no audio for,
    so reserving time for one would shift every later caption off its narration.
    """
    words: list[WordTiming] = []
    offset = 0.0
    for scene in project.scenes:
        if scene.duration_s is None:
            continue
        words.extend(
            word.model_copy(
                update={"start_s": word.start_s + offset, "end_s": word.end_s + offset}
            )
            for word in scene.words
        )
        # Exactly the segment length `assemble` builds for this scene. The two must
        # agree: see `SCENE_GAP_S`.
        offset += scene.duration_s + SCENE_GAP_S
    return words


def aspect_hash(project: Project, aspect: Aspect) -> str:
    return hash_inputs(
        scenes=[
            {
                "id": scene.id,
                # Durations decide the offsets, so they are inputs as much as the words.
                "duration_s": scene.duration_s,
                "words": [word.model_dump() for word in scene.words],
            }
            for scene in project.scenes
        ],
        style=asdict(STYLES[aspect]),
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
        words = timeline_words(project)
        if not words:
            # Nothing aligned yet: the runner aligns before it captions, so this is a
            # no-op, never an error — and an empty file would only mislead `render`.
            skipped += 1
            continue
        if not deps.stage_cache.is_stale(key, current) and path.is_file():
            skipped += 1
            continue

        write_ass(words, STYLES[aspect], path, play_res=PLAY_RES[aspect])
        deps.stage_cache.mark(key, current)
        changed = True

    if changed:
        # Nothing on the project itself changed — the artefact is the file — so only
        # the stage cache is persisted.
        deps.stage_cache.save()
    return StageResult(changed=changed, skipped_units=skipped)
