"""Splitting, merging, reordering and deleting scenes.

**The test this file exists for is `test_reorder_changes_no_per_scene_stage_hash`.**
Design decision 5 says scene ids are stable and are *not* positions: `s01…` are
handed out once and never re-flowed. It is a one-line temptation to renumber the
list after a move — and it would be a disaster, because every per-scene cache key
(`voice:s02`, `align:s02`, `visuals:s02`, `assemble:wide:s02`) and every per-scene
artefact path (`scenes/s02/narration.wav`) is keyed by that id. Renumbering would
make dragging two scenes past each other re-voice, re-align, re-fetch and
re-encode the entire video. So the reorder tests pin three separate things:

* the ids that come back are the ids that went in, still attached to the same
  narrations and the same audio;
* the per-scene fingerprints `runner.STAGE_UNITS` computes are *byte-identical*
  either side of the move — while the aspect-level `assemble` fingerprint does
  move, which is what stops that assertion from being vacuously true (only the
  concat order and the final render genuinely depend on scene order);
* the project folder is not touched at all: same files, same bytes, same inodes,
  same stage cache.

The rest is bookkeeping that is easy to get subtly wrong: a split must give the
new half a *fresh* id (never a renumber) and invalidate the original, whose
narration just got shorter; a merge must keep the first id and take the second's
artefacts off disk rather than orphaning them; and every operation has to leave
`project.scenes` ids unique, because a duplicate id silently aliases two scenes
onto one cache key and one folder.

No HTTP here, and no pipeline: the artefacts are written by hand from the same
constants the stages use, so a renamed artefact breaks this file loudly.
"""

import json
from pathlib import Path

import pytest

from videomaker.cache import StageCache, stage_key
from videomaker.models import Aspect, AssetRef, Project, Scene, SceneVisual, WordTiming
from videomaker.pipeline.align import WORDS_FILENAME
from videomaker.pipeline.assemble import segment_relpath
from videomaker.pipeline.voice import NARRATION_FILENAME
from videomaker.project import ProjectStore
from videomaker.runner import STAGE_UNITS, STAGES_RELPATH, status_key
from videomaker.scenes import delete_scene, merge_scenes, reorder_scenes, split_scene

#: The stages that own one cache unit per scene. Order-independent, every one.
PER_SCENE_STAGES = ("voice", "align", "visuals")

NARRATIONS = {
    "s01": "solid state drives keep everything in flash cells",
    "s02": "a controller maps logical blocks onto physical pages every single time",
    "s03": "erasing happens a whole block at a time",
}


@pytest.fixture
def store(tmp_path) -> ProjectStore:
    return ProjectStore(tmp_path / "workspace")


def _scene(sid: str) -> Scene:
    """A scene as the pipeline would leave it: voiced, aligned and shot."""
    return Scene(
        id=sid,
        narration=NARRATIONS[sid],
        visual=SceneVisual(
            query=f"{sid} b roll",
            # `visuals` counts a scene as produced only once something has been
            # chosen and downloaded, so this has to be here for the invalidation
            # assertions to mean anything.
            chosen=AssetRef(
                provider="mock",
                source_id=sid,
                source_url=f"mock://{sid}",
                local_path=f"scenes/{sid}/asset.mp4",
                width=1920,
                height=1080,
            ),
        ),
        audio_path=f"scenes/{sid}/{NARRATION_FILENAME}",
        duration_s=3.5,
        # `align` counts a scene as produced only when it has words, so a scene
        # with none could never be "current" and the invalidation tests below
        # would pass no matter what this module did.
        words=[
            WordTiming(word=word, start_s=index * 0.3, end_s=index * 0.3 + 0.3)
            for index, word in enumerate(NARRATIONS[sid].split())
        ],
    )


def _project(store: ProjectStore) -> Project:
    project = store.create("how ssds work", "tech_explainer", target_minutes=0.5)
    project.scenes = [_scene(sid) for sid in NARRATIONS]
    store.save(project)
    return project


def _write_artifacts(store: ProjectStore, project: Project) -> None:
    """Every per-scene file the stages produce, written from their own constants."""
    root = store.path_for(project.id)
    for scene in project.scenes:
        scene_dir = root / "scenes" / scene.id
        scene_dir.mkdir(parents=True, exist_ok=True)
        (scene_dir / NARRATION_FILENAME).write_bytes(b"riff-ish")
        (scene_dir / WORDS_FILENAME).write_text("[]")
        (scene_dir / "asset.mp4").write_bytes(b"mp4-ish")
        for aspect in Aspect:
            segment = root / segment_relpath(scene.id, aspect)
            segment.parent.mkdir(parents=True, exist_ok=True)
            segment.write_bytes(b"segment")


def _cache(store: ProjectStore, project: Project) -> StageCache:
    return StageCache(store.path_for(project.id) / STAGES_RELPATH)


def _stamp(project: Project, cache: StageCache) -> StageCache:
    """Mark every per-scene unit current, the way a finished run would."""
    for stage in PER_SCENE_STAGES:
        for unit in STAGE_UNITS[stage](project):
            cache.mark(status_key(stage, unit.unit), unit.fingerprint)
            cache.mark(stage_key(stage, unit.unit), unit.fingerprint)
    for scene in project.scenes:
        for aspect in Aspect:
            cache.mark(stage_key("assemble", f"{aspect.value}:{scene.id}"), "seg")
    cache.save()
    return cache


def _cache_keys(store: ProjectStore, project: Project) -> list[str]:
    """The stage cache as it is *on disk*, so an invalidation that was never saved
    cannot pass."""
    return list(json.loads((store.path_for(project.id) / STAGES_RELPATH).read_text()))


def _fingerprints(project: Project, stage: str) -> dict[str, str]:
    return {unit.unit: unit.fingerprint for unit in STAGE_UNITS[stage](project)}


def _current(project: Project, cache: StageCache, stage: str, scene_id: str) -> bool:
    """Exactly the runner's own currency question, for one scene of one stage."""
    unit = next(u for u in STAGE_UNITS[stage](project) if u.unit == scene_id)
    return unit.produced and not cache.is_stale(status_key(stage, unit.unit), unit.fingerprint)


def _inventory(root: Path) -> dict[str, tuple[bytes, int]]:
    """Every file under `root`: its bytes and its inode.

    The inode matters because `ProjectStore.save` and `StageCache.save` are both
    atomic `os.replace`, so a rewrite that happens to produce identical bytes
    still lands on a new inode.
    """
    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_ino)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _ids(project: Project) -> list[str]:
    return [scene.id for scene in project.scenes]


# --------------------------------------------------------------------- reorder


def test_reorder_keeps_every_scene_id_and_its_content(store):
    project = _project(store)
    before = {scene.id: scene.model_copy(deep=True) for scene in project.scenes}

    moved = reorder_scenes(project, ["s03", "s01", "s02"])

    assert _ids(moved) == ["s03", "s01", "s02"]
    for scene in moved.scenes:
        # Same id, still carrying its own narration and its own audio: an id that
        # travelled with the position instead of the scene would fail here.
        assert scene.narration == before[scene.id].narration
        assert scene.audio_path == before[scene.id].audio_path


def test_reorder_changes_no_per_scene_stage_hash(store):
    """Decision 5, stated as a hash: moving a scene re-runs nothing that is its own."""
    project = _project(store)
    before = {stage: _fingerprints(project, stage) for stage in PER_SCENE_STAGES}

    moved = reorder_scenes(project, ["s02", "s03", "s01"])

    assert {stage: _fingerprints(moved, stage) for stage in PER_SCENE_STAGES} == before
    # ...and the guard against a vacuous assertion: the things that genuinely do
    # depend on order — the concat list and the render built from it — *must* move.
    assert _fingerprints(moved, "assemble") != _fingerprints(project, "assemble")
    assert _fingerprints(moved, "render") != _fingerprints(project, "render")


def test_reorder_touches_nothing_on_disk(store):
    project = _project(store)
    _write_artifacts(store, project)
    _stamp(project, _cache(store, project))
    root = store.path_for(project.id)
    before = _inventory(root)

    reorder_scenes(project, ["s02", "s01", "s03"])

    assert _inventory(root) == before


def test_reorder_rejects_an_id_set_that_is_not_the_project(store):
    project = _project(store)
    for bad in (["s01", "s02"], ["s01", "s02", "s03", "s04"], ["s01", "s02", "s02"]):
        with pytest.raises(ValueError, match="reorder"):
            reorder_scenes(project, bad)
    assert _ids(project) == ["s01", "s02", "s03"]


# ----------------------------------------------------------------------- split


def test_split_produces_two_scenes_whose_narrations_concatenate_back(store):
    project = _project(store)
    original = project.scene_by_id("s02").narration

    split = split_scene(project, "s02", 4)

    assert _ids(split) == ["s01", "s02", "s04", "s03"]
    head = split.scene_by_id("s02").narration
    tail = split.scene_by_id("s04").narration
    assert f"{head} {tail}" == original
    assert len(head.split()) == 4


def test_split_takes_a_fresh_id_rather_than_renumbering(store):
    project = _project(store)

    split = split_scene(project, "s01", 3)

    # The new half is `s04`, not `s02`: renumbering would move every later scene's
    # cache keys and artefacts onto a different id.
    assert _ids(split) == ["s01", "s04", "s02", "s03"]
    assert split.scene_by_id("s02").narration == NARRATIONS["s02"]


@pytest.mark.parametrize("at_word", [0, -1, 8, 9, 40])
def test_split_rejects_word_zero_or_past_the_end(store, at_word):
    project = _project(store)  # s01 is eight words long

    with pytest.raises(ValueError, match="at_word"):
        split_scene(project, "s01", at_word)
    assert _ids(project) == ["s01", "s02", "s03"]


def test_split_invalidates_the_original_and_nothing_else(store):
    project = _project(store)
    _write_artifacts(store, project)
    cache = _stamp(project, _cache(store, project))

    split = split_scene(project, "s02", 4, store=store, stage_cache=cache)

    for stage in PER_SCENE_STAGES:
        assert not _current(split, cache, stage, "s02"), stage
        assert _current(split, cache, stage, "s01"), stage
        assert _current(split, cache, stage, "s03"), stage
    # The take no longer matches the words, so it is gone from the scene as well
    # as from the cache — and so is the recording it was measured from.
    scene = split.scene_by_id("s02")
    assert scene.audio_path is None
    assert scene.duration_s is None
    scene_dir = store.path_for(project.id) / "scenes" / "s02"
    assert not (scene_dir / NARRATION_FILENAME).exists()
    assert not (scene_dir / WORDS_FILENAME).exists()
    assert (store.path_for(project.id) / "scenes" / "s01" / NARRATION_FILENAME).exists()


def test_split_leaves_the_new_half_with_no_borrowed_artifacts(store):
    project = _project(store)

    split = split_scene(project, "s02", 4)

    fresh = split.scene_by_id("s04")
    assert fresh.audio_path is None
    assert fresh.duration_s is None
    assert fresh.words == []
    assert fresh.visual.chosen is None
    assert fresh.visual.query == project.scene_by_id("s02").visual.query


# ----------------------------------------------------------------------- merge


def test_merge_concatenates_in_order_and_drops_the_second_id(store):
    project = _project(store)

    merged = merge_scenes(project, "s01", "s02")

    assert _ids(merged) == ["s01", "s03"]
    assert merged.scene_by_id("s01").narration == f"{NARRATIONS['s01']} {NARRATIONS['s02']}"


def test_merge_keeps_the_first_id_and_deletes_the_seconds_artifacts(store):
    project = _project(store)
    _write_artifacts(store, project)
    cache = _stamp(project, _cache(store, project))
    root = store.path_for(project.id)

    merge_scenes(project, "s02", "s03", store=store, stage_cache=cache)

    assert not (root / "scenes" / "s03").exists()
    for aspect in Aspect:
        assert not (root / segment_relpath("s03", aspect)).exists()
    # No orphan cache entry either: a key for a scene that no longer exists is a
    # stale hash waiting to be matched by a future scene of the same id.
    assert not [key for key in _cache_keys(store, project) if "s03" in key]


def test_merge_invalidates_the_surviving_scene(store):
    project = _project(store)
    _write_artifacts(store, project)
    cache = _stamp(project, _cache(store, project))

    merged = merge_scenes(project, "s01", "s02", store=store, stage_cache=cache)

    for stage in PER_SCENE_STAGES:
        assert not _current(merged, cache, stage, "s01"), stage
        assert _current(merged, cache, stage, "s03"), stage


def test_merge_rejects_a_scene_merged_with_itself(store):
    project = _project(store)
    with pytest.raises(ValueError, match="itself"):
        merge_scenes(project, "s02", "s02")


# ---------------------------------------------------------------------- delete


def test_delete_removes_the_scene_and_its_artifacts(store):
    project = _project(store)
    _write_artifacts(store, project)
    cache = _stamp(project, _cache(store, project))
    root = store.path_for(project.id)

    remaining = delete_scene(project, "s02", store=store, stage_cache=cache)

    assert _ids(remaining) == ["s01", "s03"]
    assert not (root / "scenes" / "s02").exists()
    for aspect in Aspect:
        assert not (root / segment_relpath("s02", aspect)).exists()
    assert not [key for key in _cache_keys(store, project) if "s02" in key]
    # The scenes either side of the hole keep everything they had.
    assert (root / "scenes" / "s01" / NARRATION_FILENAME).exists()
    assert (root / "scenes" / "s03" / NARRATION_FILENAME).exists()
    assert _current(remaining, cache, "voice", "s03")


# ------------------------------------------------------------ shared invariants


def test_an_unknown_scene_id_is_a_key_error(store):
    project = _project(store)
    for call in (
        lambda: split_scene(project, "s09", 2),
        lambda: merge_scenes(project, "s09", "s01"),
        lambda: merge_scenes(project, "s01", "s09"),
        lambda: delete_scene(project, "s09"),
    ):
        with pytest.raises(KeyError):
            call()


def test_every_operation_leaves_the_ids_unique(store):
    project = _project(store)
    steps = [
        lambda p: split_scene(p, "s02", 4),
        lambda p: reorder_scenes(p, list(reversed(_ids(p)))),
        lambda p: merge_scenes(p, "s01", "s03"),
        lambda p: split_scene(p, "s01", 2),
        lambda p: delete_scene(p, "s04"),
    ]
    for step in steps:
        project = step(project)
        ids = _ids(project)
        assert len(ids) == len(set(ids)), ids


def test_no_operation_mutates_the_project_it_was_given(store):
    project = _project(store)
    before = project.model_dump_json()

    split_scene(project, "s02", 4)
    merge_scenes(project, "s01", "s02")
    reorder_scenes(project, ["s03", "s02", "s01"])
    delete_scene(project, "s01")

    assert project.model_dump_json() == before
