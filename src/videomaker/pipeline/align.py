"""The align stage: word-level timings for every scene's narration.

Whisper is asked to transcribe the narration it just heard, and the result is snapped
back onto the script with `videomaker.align.snap_to_script` — captions must carry the
writer's spelling and punctuation with whisper's timings, never whisper's spelling.

The cache unit is the scene, and the hash covers the audio's *content*, so a scene
re-voiced with the same words still gets re-aligned while its neighbours do not.
"""

import json
from pathlib import Path

from videomaker.align import snap_to_script
from videomaker.cache import hash_inputs, stage_key
from videomaker.models import Project, Scene, WordTiming
from videomaker.pipeline.base import (
    StageDeps,
    StageResult,
    call_chain,
    content_hash,
    project_root,
)
from videomaker.providers.base import STTProvider

STAGE = "align"
WORDS_FILENAME = "words.json"


def scene_hash(scene: Scene, audio_path: Path, provider: str) -> str:
    return hash_inputs(
        narration=scene.narration,
        audio=content_hash(audio_path),
        provider=provider,
    )


def _transcribe(deps: StageDeps, project: Project, scene: Scene, audio_path: Path):
    def call(_name: str, stt: STTProvider) -> list[WordTiming]:
        return stt.transcribe_words(
            audio_path=audio_path,
            language=project.language,
            # The script is a strong prior: whisper mishears proper nouns (M0).
            hint_text=scene.narration,
        )

    return call_chain(deps, "stt", call)


def _write_words(path: Path, words: list[WordTiming]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [word.model_dump() for word in words]
    path.write_text(json.dumps(payload, indent=2))


def run_align(project: Project, deps: StageDeps) -> StageResult:
    """Time every stale, unlocked, voiced scene. One unit per scene: `align:sNN`."""
    provider = deps.leading_name("stt")
    root = project_root(deps, project)
    changed = False
    skipped = 0

    for scene in project.scenes:
        audio_path = root / scene.audio_path if scene.audio_path else None
        if scene.locked or audio_path is None or not audio_path.is_file():
            # Nothing to align yet (or nothing we are allowed to touch): the runner
            # voices before it aligns, so this is a no-op, never an error.
            skipped += 1
            continue

        key = stage_key(STAGE, scene.id)
        current = scene_hash(scene, audio_path, provider)
        words_path = deps.store.scene_dir(project, scene.id) / WORDS_FILENAME
        if not deps.stage_cache.is_stale(key, current) and words_path.is_file() and scene.words:
            skipped += 1
            continue

        heard = _transcribe(deps, project, scene, audio_path)
        # `duration_s` is the length `voice` measured for this very file, so it is
        # the right upper anchor for a trailing run of words whisper never heard.
        words = snap_to_script(
            scene.narration, heard, audio_duration_s=scene.duration_s
        )
        _write_words(words_path, words)
        scene.words = words
        deps.stage_cache.mark(key, current)
        changed = True

    if changed:
        deps.stage_cache.save()
        deps.store.save(project)
    return StageResult(changed=changed, skipped_units=skipped)
