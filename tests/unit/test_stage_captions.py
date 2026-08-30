"""The captions stage: every scene's words on one project-wide timeline.

`test_scene_two_starts_at_scene_one_plus_the_gap` is the test this stage exists for.
Each scene's words are timed from zero *within that scene*, so concatenating them
without adding the running offset produces captions that are perfect for scene one
and progressively wrong for everything after it — a drift no per-scene test can see.
"""

from itertools import pairwise

import pytest

from videomaker.cache import ResponseCache, StageCache, stage_key
from videomaker.config import Settings
from videomaker.media.ass import STYLES, format_timestamp
from videomaker.models import Aspect, Scene, SceneVisual, WordTiming
from videomaker.pipeline.base import SCENE_GAP_S, StageDeps
from videomaker.pipeline.captions import (
    caption_relpath,
    run_captions,
    timeline_words,
)
from videomaker.project import ProjectStore
from videomaker.providers.ratelimit import QuotaTracker

WORDS_PER_SCENE = 5  # == STYLES[WIDE].words_per_chunk, so scene boundaries start chunks
SCENES = {
    "s01": ("Solid state drives keep bytes", 2.0),
    "s02": ("A flash cell traps electrons", 3.0),
    "s03": ("Nothing moves so reads finish", 1.5),
}


@pytest.fixture
def deps(tmp_path):
    settings = Settings(workspace_dir=tmp_path / "workspace", provider_chains={})
    return StageDeps(
        settings=settings,
        store=ProjectStore(settings.workspace_dir),
        stage_cache=StageCache(tmp_path / "stages.json"),
        response_cache=ResponseCache(tmp_path / "responses"),
        quota=QuotaTracker(tmp_path / "quota.json"),
    )


def _words(narration: str, duration_s: float) -> list[WordTiming]:
    """Zero-gap timings from zero to `duration_s`, exactly as `align` leaves them."""
    parts = narration.split()
    step = duration_s / len(parts)
    return [
        WordTiming(word=word, start_s=index * step, end_s=(index + 1) * step)
        for index, word in enumerate(parts)
    ]


def _project(deps, *, aligned: bool = True):
    project = deps.store.create("how ssds work", "tech_explainer", target_minutes=1.0)
    project.scenes = [
        Scene(
            id=sid,
            narration=narration,
            visual=SceneVisual(query=f"{sid} b-roll"),
            audio_path=f"scenes/{sid}/narration.wav",
            duration_s=duration_s,
            words=_words(narration, duration_s) if aligned else [],
        )
        for sid, (narration, duration_s) in SCENES.items()
    ]
    deps.store.save(project)
    return project


def _ass_path(deps, project, aspect=Aspect.WIDE):
    return deps.store.path_for(project.id) / caption_relpath(aspect)


def _dialogue(path):
    """`(start, end, text)` for every Dialogue line in an .ass file."""
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("Dialogue:"):
            continue
        fields = line[len("Dialogue:") :].split(",", 9)
        lines.append((fields[1].strip(), fields[2].strip(), fields[9]))
    return lines


# ------------------------------------------------------------------ the offset


def test_scene_two_starts_at_scene_one_plus_the_gap(deps):
    project = _project(deps)

    words = timeline_words(project)

    first_of_scene_two = words[WORDS_PER_SCENE]
    assert first_of_scene_two.word == "A"
    # Not its within-scene 0.0: scene two starts where scene one's segment ends.
    assert first_of_scene_two.start_s == pytest.approx(SCENES["s01"][1] + SCENE_GAP_S)
    first_of_scene_three = words[2 * WORDS_PER_SCENE]
    assert first_of_scene_three.start_s == pytest.approx(
        SCENES["s01"][1] + SCENES["s02"][1] + 2 * SCENE_GAP_S
    )


def test_the_offset_reaches_the_ass_file(deps):
    project = _project(deps)
    run_captions(project, deps)

    lines = _dialogue(_ass_path(deps, project))

    assert len(lines) == 3  # five words per scene, five words per chunk
    assert lines[0][0] == format_timestamp(0.0)
    assert lines[1][0] == format_timestamp(SCENES["s01"][1] + SCENE_GAP_S)
    assert lines[1][2] == SCENES["s02"][0]


def test_timeline_words_are_monotonic_across_scene_boundaries(deps):
    project = _project(deps)

    words = timeline_words(project)

    assert len(words) == 3 * WORDS_PER_SCENE
    for previous, following in pairwise(words):
        assert previous.start_s <= following.start_s
        assert previous.end_s <= following.start_s + 1e-9


def test_unvoiced_scenes_do_not_shift_the_timeline(deps):
    """A scene with no audio produces no segment, so it must not consume time."""
    project = _project(deps)
    scene = project.scene_by_id("s02")
    scene.duration_s = None
    scene.words = []

    words = timeline_words(project)

    assert words[WORDS_PER_SCENE].start_s == pytest.approx(SCENES["s01"][1] + SCENE_GAP_S)


# ------------------------------------------------------------------- the stage


def test_captions_writes_one_ass_file_per_aspect(deps):
    project = _project(deps)

    result = run_captions(project, deps)

    path = _ass_path(deps, project)
    assert result.changed is True
    assert caption_relpath(Aspect.WIDE) == "captions/wide.ass"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "[Events]" in text
    assert "PlayResX: 1920" in text
    assert f",{STYLES[Aspect.WIDE].font_size}," in text  # the wide style, not a scaled one


def test_clean_rerun_rewrites_nothing(deps):
    project = _project(deps)
    run_captions(project, deps)
    path = _ass_path(deps, project)
    mtime = path.stat().st_mtime_ns

    result = run_captions(project, deps)

    assert result.changed is False
    assert result.skipped_units == 1
    assert path.stat().st_mtime_ns == mtime


def test_editing_one_scenes_words_rewrites_the_whole_file(deps):
    """The unit is the aspect: one file covers every scene, so any edit rewrites it."""
    project = _project(deps)
    run_captions(project, deps)

    project.scene_by_id("s02").words = _words("Electrons sit behind an insulator", 3.0)
    result = run_captions(project, deps)

    assert result.changed is True
    assert "Electrons sit behind an insulator" in _ass_path(deps, project).read_text()


def test_changing_a_scene_duration_reflows_later_captions(deps):
    project = _project(deps)
    run_captions(project, deps)

    project.scene_by_id("s01").duration_s = 9.0
    result = run_captions(project, deps)

    assert result.changed is True
    lines = _dialogue(_ass_path(deps, project))
    assert lines[1][0] == format_timestamp(9.0 + SCENE_GAP_S)


def test_a_deleted_ass_file_is_rewritten_even_though_the_hash_is_fresh(deps):
    project = _project(deps)
    run_captions(project, deps)
    _ass_path(deps, project).unlink()

    result = run_captions(project, deps)

    assert result.changed is True
    assert _ass_path(deps, project).is_file()


def test_captions_skips_a_project_with_nothing_aligned(deps):
    project = _project(deps, aligned=False)

    result = run_captions(project, deps)

    assert result.changed is False
    assert result.skipped_units == 1
    assert not _ass_path(deps, project).exists()


def test_captions_marks_its_unit_per_aspect(deps):
    project = _project(deps)
    run_captions(project, deps)

    persisted = StageCache(deps.stage_cache.path)
    assert persisted.is_stale(stage_key("captions", Aspect.WIDE.value), "not-the-real-hash") is True
    assert run_captions(project, deps).skipped_units == 1
