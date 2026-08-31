"""Editing the *shape* of a script: splitting, merging, reordering, deleting.

**Scene ids are stable and are not positions** (M2 design decision 5). `s01…` are
handed out once, by `Project.next_scene_id`, and never re-flowed. Everything a
scene owns is keyed by that id — its cache units (`voice:s02`, `align:s02`,
`visuals:s02`, `assemble:wide:s02`), its folder (`scenes/s02/`), its encoded
segment (`build/s02_wide.mp4`) — so renumbering the list after a move would point
every one of those keys at a different scene's work. The user would drag scene
five above scene four and watch the whole video re-voice, re-align, re-fetch and
re-encode. `reorder_scenes` therefore changes the order of `project.scenes` and
nothing else at all: it takes no store and no stage cache, because there is
genuinely nothing for it to invalidate. What legitimately depends on order — the
concat list and the render built from it — is already covered by the aspect-level
`assemble`/`render` fingerprints, which hash the scene list; they go stale on
their own, without help from here.

The other three operations rewrite a narration, and a rewritten narration means
the take on disk no longer says the words the scene claims. So they do three
things together, and all three matter:

1. clear the derived fields (`audio_path`, `duration_s`, `words`) on any scene
   whose text changed, so the project stops advertising work that is gone;
2. drop that scene's per-scene cache entries, so the runner re-runs it;
3. delete the artefacts of any scene that ceased to exist, so a merged-away or
   deleted id leaves no orphan wav, words file or segment behind — an orphan is
   not just wasted space, it is a file a future scene of the same id could find
   and mistake for its own.

Steps 2 and 3 need a `ProjectStore` and a `StageCache`, which the caller may not
have (the tests that only care about the arithmetic do not). They are keyword-only
and optional: pass them and the disk is kept honest, omit them and the function is
pure.

Every operation returns a **new** `Project`; the one passed in is never mutated.
That is what lets a route decide to save — or not — after the fact, and it keeps
"what changed" a diff rather than a side effect.
"""

import re
import shutil
from pathlib import Path

from videomaker.cache import StageCache, stage_key
from videomaker.models import Aspect, Project, Scene, SceneVisual
from videomaker.pipeline.align import WORDS_FILENAME
from videomaker.pipeline.assemble import segment_relpath
from videomaker.pipeline.voice import NARRATION_FILENAME
from videomaker.project import ProjectStore
from videomaker.runner import status_key

#: The stages that own one cache unit per scene id. None of them depends on where
#: the scene sits in the list — which is the whole of decision 5, in one tuple.
PER_SCENE_STAGES: tuple[str, ...] = ("voice", "align", "visuals")

#: The scene's own folder, relative to the project root.
SCENES_DIRNAME = "scenes"

#: A word, for the purposes of a split. Deliberately the same notion of "word"
#: `str.split()` uses (and therefore the same one the page's word count shows),
#: but matched by span so the halves keep the narration's own line breaks rather
#: than being reflowed into one line by a `" ".join`.
_WORD = re.compile(r"\S+")


# ------------------------------------------------------------------- internals


def _cache_prefixes(scene_id: str) -> tuple[str, ...]:
    """Every stage-cache key that belongs to one scene.

    Both spellings are needed: the stages key their own work as `voice:s02`, and
    `runner` keeps a parallel `status:voice:s02` for the status view. Leaving
    either behind would let a scene look finished to one of them and stale to the
    other. `assemble` is included at its per-scene granularity only — the
    aspect-level entry belongs to the whole timeline, not to this scene.
    """
    keys = [stage_key(stage, scene_id) for stage in PER_SCENE_STAGES]
    keys += [status_key(stage, scene_id) for stage in PER_SCENE_STAGES]
    keys += [stage_key("assemble", f"{aspect.value}:{scene_id}") for aspect in Aspect]
    return tuple(keys)


def _invalidate(stage_cache: StageCache | None, scene_id: str) -> None:
    """Forget what we know about one scene, so the runner rebuilds it."""
    if stage_cache is None:
        return
    for prefix in _cache_prefixes(scene_id):
        stage_cache.invalidate(prefix)
    stage_cache.save()


def _scene_dir(store: ProjectStore, project: Project, scene_id: str) -> Path:
    """The scene's folder — *without* creating it, unlike `ProjectStore.scene_dir`."""
    return store.path_for(project.id) / SCENES_DIRNAME / scene_id


def _drop_recording(store: ProjectStore | None, project: Project, scene_id: str) -> None:
    """Remove the narration and its alignment: the words they hold have changed.

    The chosen visual is deliberately left where it is. Its query has not moved,
    so re-downloading it would be a network round trip to fetch the same bytes;
    the cache entry is invalidated either way, which is what "stale" means.
    """
    if store is None:
        return
    scene_dir = _scene_dir(store, project, scene_id)
    for name in (NARRATION_FILENAME, WORDS_FILENAME):
        (scene_dir / name).unlink(missing_ok=True)


def _delete_artifacts(store: ProjectStore | None, project: Project, scene_id: str) -> None:
    """Remove everything on disk that belonged to a scene that no longer exists."""
    if store is None:
        return
    shutil.rmtree(_scene_dir(store, project, scene_id), ignore_errors=True)
    root = store.path_for(project.id)
    for aspect in Aspect:
        (root / segment_relpath(scene_id, aspect)).unlink(missing_ok=True)


def _reset(scene: Scene) -> None:
    """Forget the take: this scene's narration is not the one that was spoken.

    `locked` goes with it. A lock means "an editor chose this take deliberately",
    and both `voice` and `align` honour it over staleness — so leaving it set on a
    scene whose words just changed would pin the old recording against the new
    text permanently, and silently.
    """
    scene.audio_path = None
    scene.duration_s = None
    scene.words = []
    scene.locked = False
    scene.error = None


def _copy(project: Project) -> Project:
    return project.model_copy(deep=True)


def _index_of(project: Project, scene_id: str) -> int:
    for index, scene in enumerate(project.scenes):
        if scene.id == scene_id:
            return index
    raise KeyError(scene_id)


# ------------------------------------------------------------------ operations


def split_scene(
    project: Project,
    scene_id: str,
    at_word: int,
    *,
    store: ProjectStore | None = None,
    stage_cache: StageCache | None = None,
) -> Project:
    """Cut one scene in two at `at_word`, the new half taking a **fresh** id.

    `at_word` is a word boundary counted from the start of the narration, so it
    has to leave words on both sides: 0 (or less) and anything from the word count
    upwards are rejected rather than quietly clamped — a "split" that produced an
    empty scene would be a delete wearing a disguise.

    The new half is `next_scene_id()`, never a renumber of what follows it, and it
    starts with no artefacts of its own: it inherits the search query and the
    framing, because those describe an intent the editor already expressed, but
    not the chosen asset, whose file lives under the *original's* id.
    """
    result = _copy(project)
    index = _index_of(result, scene_id)
    original = result.scenes[index]

    words = list(_WORD.finditer(original.narration))
    if at_word < 1 or at_word >= len(words):
        raise ValueError(
            f"at_word must leave words on both sides of the split "
            f"(1..{len(words) - 1} for {scene_id}), got {at_word}"
        )
    cut = words[at_word].start()
    head, tail_text = original.narration[:cut].strip(), original.narration[cut:].strip()

    tail = Scene(
        id=result.next_scene_id(),
        narration=tail_text,
        visual=SceneVisual(
            query=original.visual.query,
            alt_queries=list(original.visual.alt_queries),
            kind=original.visual.kind,
            motion=original.visual.motion,
            crop_focus_x=original.visual.crop_focus_x,
        ),
        in_short=original.in_short,
    )
    original.narration = head
    _reset(original)
    result.scenes.insert(index + 1, tail)

    _drop_recording(store, result, original.id)
    _invalidate(stage_cache, original.id)
    return result


def merge_scenes(
    project: Project,
    first_id: str,
    second_id: str,
    *,
    store: ProjectStore | None = None,
    stage_cache: StageCache | None = None,
) -> Project:
    """Join two scenes into the **first** one, in the order given.

    The first id survives, in the first scene's position, carrying the joined
    narration and the first scene's visual. The second id stops existing, so its
    folder and its encoded segments go with it: leaving them would orphan real
    megabytes, and worse, would leave a `scenes/s03/narration.wav` for a future
    `s03` to find.
    """
    if first_id == second_id:
        raise ValueError(f"a scene cannot be merged with itself: {first_id}")
    result = _copy(project)
    first = result.scenes[_index_of(result, first_id)]
    second = result.scenes[_index_of(result, second_id)]

    first.narration = f"{first.narration.strip()} {second.narration.strip()}".strip()
    _reset(first)
    result.scenes.remove(second)

    _drop_recording(store, result, first.id)
    _invalidate(stage_cache, first.id)
    _delete_artifacts(store, result, second.id)
    _invalidate(stage_cache, second.id)
    return result


def reorder_scenes(project: Project, ordered_ids: list[str]) -> Project:
    """Put the scenes in the given order. **Nothing else changes — by design.**

    No store, no stage cache, no disk: every id keeps its artefacts and its cache
    entries, so moving a scene re-encodes nothing that belongs to a scene. Only
    the concat list and the final render depend on order, and they notice through
    their own fingerprints.

    `ordered_ids` must be a permutation of the project's ids — not a subset, not a
    superset, no repeats. A partial list would be a silent delete.
    """
    existing = [scene.id for scene in project.scenes]
    if sorted(ordered_ids) != sorted(existing):
        raise ValueError(
            f"reorder needs every scene id exactly once: expected {existing}, got {ordered_ids}"
        )
    result = _copy(project)
    by_id = {scene.id: scene for scene in result.scenes}
    result.scenes = [by_id[scene_id] for scene_id in ordered_ids]
    return result


def delete_scene(
    project: Project,
    scene_id: str,
    *,
    store: ProjectStore | None = None,
    stage_cache: StageCache | None = None,
) -> Project:
    """Remove one scene and everything that was ever built for it.

    The scenes either side are untouched — same ids, same artefacts, same cache
    entries — so deleting scene two costs exactly scene two.
    """
    result = _copy(project)
    del result.scenes[_index_of(result, scene_id)]
    _delete_artifacts(store, result, scene_id)
    _invalidate(stage_cache, scene_id)
    return result
