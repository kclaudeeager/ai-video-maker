"""The voice stage: one narration wav per scene.

Per-scene narration is what makes the rest of the pipeline cheap — each scene has an
exact measured duration, so visuals cut to audio with no alignment guesswork (M0
finding 7) — and it is what makes an edit cheap: the cache unit is the scene, so
rewriting scene two re-synthesises scene two and nothing else.
"""

from pathlib import Path

from videomaker.cache import hash_inputs, stage_key
from videomaker.media.ffmpeg import probe_duration
from videomaker.models import Project, Scene
from videomaker.pipeline.base import StageDeps, StageResult, call_chain, relative_to_project
from videomaker.providers.base import TTSProvider

STAGE = "voice"
NARRATION_FILENAME = "narration.wav"
#: Narration speed is a project-wide dial in M3; M1 always speaks at 1.0.
DEFAULT_SPEED = 1.0


def scene_hash(project: Project, scene: Scene, provider: str) -> str:
    return hash_inputs(
        narration=scene.narration,
        voice=project.voice,
        speed=DEFAULT_SPEED,
        provider=provider,
    )


def _is_fresh(deps: StageDeps, project: Project, scene: Scene, key: str, current: str) -> bool:
    """Fresh means the hash matches *and* the wav is still on disk."""
    if deps.stage_cache.is_stale(key, current) or scene.audio_path is None:
        return False
    return (deps.store.path_for(project.id) / scene.audio_path).is_file()


def _synthesize(deps: StageDeps, project: Project, scene: Scene, out_path: Path) -> None:
    def call(_name: str, tts: TTSProvider) -> None:
        tts.synthesize(
            text=scene.narration,
            voice=project.voice,
            out_path=out_path,
            speed=DEFAULT_SPEED,
            language=project.language,
        )

    call_chain(deps, "tts", call)


def run_voice(project: Project, deps: StageDeps) -> StageResult:
    """Speak every stale, unlocked scene. One unit per scene: `voice:sNN`."""
    provider = deps.leading_name("tts")
    changed = False
    skipped = 0

    for scene in project.scenes:
        key = stage_key(STAGE, scene.id)
        if scene.locked:
            # An editor locked this take deliberately; staleness does not outrank that.
            skipped += 1
            continue
        current = scene_hash(project, scene, provider)
        if _is_fresh(deps, project, scene, key, current):
            skipped += 1
            continue

        out_path = deps.store.scene_dir(project, scene.id) / NARRATION_FILENAME
        _synthesize(deps, project, scene, out_path)
        scene.audio_path = relative_to_project(deps, project, out_path)
        # Measured, not predicted: the wav on disk is what every later stage cuts to.
        scene.duration_s = probe_duration(out_path)
        scene.error = None
        deps.stage_cache.mark(key, current)
        changed = True

    if changed:
        deps.stage_cache.save()
        deps.store.save(project)
    return StageResult(changed=changed, skipped_units=skipped)
