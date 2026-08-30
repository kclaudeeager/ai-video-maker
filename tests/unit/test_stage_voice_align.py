"""The voice and align stages, and the per-scene cache scoping the DoD rests on.

`test_editing_one_scene_invalidates_only_that_scene` is the "<10 s re-run" promise
made concrete: if per-scene hash scoping ever regresses into whole-project
invalidation, every other test here still passes and only this one fails.
"""

import json

import pytest

from videomaker.cache import ResponseCache, StageCache, stage_key
from videomaker.config import Settings
from videomaker.models import Scene, SceneVisual
from videomaker.pipeline.align import WORDS_FILENAME, run_align
from videomaker.pipeline.base import StageDeps
from videomaker.pipeline.voice import NARRATION_FILENAME, run_voice
from videomaker.project import ProjectStore
from videomaker.providers.mock import MockSTT, MockTTS
from videomaker.providers.ratelimit import QuotaTracker

NARRATIONS = {
    "s01": "Solid state drives keep every byte in silicon rather than on a spinning platter.",
    "s02": "A flash cell traps electrons behind an insulator, and that trapped charge is the bit.",
    "s03": "Because nothing has to move, a read finishes in microseconds instead of milliseconds.",
}


@pytest.fixture
def mock_deps(tmp_path, monkeypatch):
    """Real mock providers, wrapped so the test can count what the stages spent."""
    settings = Settings(
        workspace_dir=tmp_path / "workspace",
        provider_chains={"tts": ["mock"], "stt": ["mock"]},
    )
    deps = StageDeps(
        settings=settings,
        store=ProjectStore(settings.workspace_dir),
        stage_cache=StageCache(tmp_path / "stages.json"),
        response_cache=ResponseCache(tmp_path / "responses"),
        quota=QuotaTracker(tmp_path / "quota.json"),
    )
    deps.tts_calls = 0
    deps.stt_calls = 0

    synthesize = MockTTS.synthesize
    transcribe = MockSTT.transcribe_words

    def counted_synthesize(self, **kwargs):
        deps.tts_calls += 1
        return synthesize(self, **kwargs)

    def counted_transcribe(self, **kwargs):
        deps.stt_calls += 1
        return transcribe(self, **kwargs)

    monkeypatch.setattr(MockTTS, "synthesize", counted_synthesize)
    monkeypatch.setattr(MockSTT, "transcribe_words", counted_transcribe)
    return deps


def _project_with_three_scenes(mock_deps):
    project = mock_deps.store.create("how ssds work", "tech_explainer", target_minutes=1.0)
    project.scenes = [
        Scene(id=sid, narration=text, visual=SceneVisual(query=f"{sid} b-roll"))
        for sid, text in NARRATIONS.items()
    ]
    mock_deps.store.save(project)
    return project


def _voiced_project_with_three_scenes(tmp_path, mock_deps):
    project = _project_with_three_scenes(mock_deps)
    run_voice(project, mock_deps)
    return project


def _scene_file(mock_deps, project, scene_id, filename):
    return mock_deps.store.path_for(project.id) / "scenes" / scene_id / filename


# ------------------------------------------------------------------------- voice


def test_voice_writes_a_wav_per_scene_with_relative_paths_and_durations(tmp_path, mock_deps):
    project = _voiced_project_with_three_scenes(tmp_path, mock_deps)

    assert mock_deps.tts_calls == 3
    for scene_id in NARRATIONS:
        scene = project.scene_by_id(scene_id)
        assert scene.audio_path == f"scenes/{scene_id}/{NARRATION_FILENAME}"
        assert not scene.audio_path.startswith("/")  # project folders must stay movable
        assert _scene_file(mock_deps, project, scene_id, NARRATION_FILENAME).is_file()
        assert scene.duration_s is not None
        assert scene.duration_s > 0


def test_clean_rerun_of_voice_makes_zero_tts_calls(tmp_path, mock_deps):
    project = _voiced_project_with_three_scenes(tmp_path, mock_deps)

    result = run_voice(project, mock_deps)

    assert mock_deps.tts_calls == 3  # unchanged
    assert result.changed is False
    assert result.skipped_units == 3


def test_editing_one_scene_invalidates_only_that_scene(tmp_path, mock_deps):
    project = _voiced_project_with_three_scenes(tmp_path, mock_deps)
    calls_before = mock_deps.tts_calls
    untouched = _scene_file(mock_deps, project, "s01", NARRATION_FILENAME)
    untouched_mtime = untouched.stat().st_mtime_ns

    project.scene_by_id("s02").narration = "Completely different narration now."
    run_voice(project, mock_deps)

    assert mock_deps.tts_calls == calls_before + 1  # only s02 re-synthesised
    assert project.scene_by_id("s01").audio_path is not None
    assert project.scene_by_id("s03").audio_path is not None
    assert untouched.stat().st_mtime_ns == untouched_mtime  # s01's wav was not rewritten


def test_locked_scene_is_never_resynthesised_even_when_stale(tmp_path, mock_deps):
    project = _voiced_project_with_three_scenes(tmp_path, mock_deps)
    scene = project.scene_by_id("s02")
    original_duration = scene.duration_s
    scene.locked = True
    scene.narration = "This narration changed, but the scene is locked."

    result = run_voice(project, mock_deps)

    assert mock_deps.tts_calls == 3  # no fourth call
    assert result.changed is False
    assert scene.duration_s == original_duration


def test_deleted_audio_is_resynthesised_even_though_the_hash_is_fresh(tmp_path, mock_deps):
    project = _voiced_project_with_three_scenes(tmp_path, mock_deps)
    _scene_file(mock_deps, project, "s03", NARRATION_FILENAME).unlink()

    result = run_voice(project, mock_deps)

    assert mock_deps.tts_calls == 4
    assert result.changed is True
    assert _scene_file(mock_deps, project, "s03", NARRATION_FILENAME).is_file()


def test_changing_the_voice_invalidates_every_scene(tmp_path, mock_deps):
    project = _voiced_project_with_three_scenes(tmp_path, mock_deps)

    project.voice = "am_adam"
    run_voice(project, mock_deps)

    assert mock_deps.tts_calls == 6


# ------------------------------------------------------------------------- align


def test_align_writes_script_words_with_measured_timings(tmp_path, mock_deps):
    project = _voiced_project_with_three_scenes(tmp_path, mock_deps)

    result = run_align(project, mock_deps)

    assert result.changed is True
    assert mock_deps.stt_calls == 3
    for scene_id, narration in NARRATIONS.items():
        scene = project.scene_by_id(scene_id)
        # Captions must show the script's words, not whisper's spelling.
        assert [word.word for word in scene.words] == narration.split()
        assert scene.words[0].start_s == pytest.approx(0.0, abs=0.01)
        assert scene.words[-1].end_s <= scene.duration_s + 0.01
        path = _scene_file(mock_deps, project, scene_id, WORDS_FILENAME)
        assert path.is_file()
        assert [entry["word"] for entry in json.loads(path.read_text())] == narration.split()


def test_clean_rerun_of_align_makes_zero_stt_calls(tmp_path, mock_deps):
    project = _voiced_project_with_three_scenes(tmp_path, mock_deps)
    run_align(project, mock_deps)

    result = run_align(project, mock_deps)

    assert mock_deps.stt_calls == 3
    assert result.changed is False
    assert result.skipped_units == 3


def test_revoicing_one_scene_realigns_only_that_scene(tmp_path, mock_deps):
    project = _voiced_project_with_three_scenes(tmp_path, mock_deps)
    run_align(project, mock_deps)

    project.scene_by_id("s02").narration = "Completely different narration now."
    run_voice(project, mock_deps)
    run_align(project, mock_deps)

    assert mock_deps.stt_calls == 4
    assert [word.word for word in project.scene_by_id("s02").words] == [
        "Completely",
        "different",
        "narration",
        "now.",
    ]
    assert [word.word for word in project.scene_by_id("s01").words] == NARRATIONS["s01"].split()


def test_locked_scene_is_never_realigned(tmp_path, mock_deps):
    project = _voiced_project_with_three_scenes(tmp_path, mock_deps)
    run_align(project, mock_deps)
    scene = project.scene_by_id("s03")
    scene.locked = True
    scene.narration = "Locked scenes keep the timings the editor approved."

    result = run_align(project, mock_deps)

    assert mock_deps.stt_calls == 3
    assert result.changed is False
    assert [word.word for word in scene.words] == NARRATIONS["s03"].split()


def test_align_skips_scenes_that_have_not_been_voiced(mock_deps):
    project = _project_with_three_scenes(mock_deps)

    result = run_align(project, mock_deps)

    assert mock_deps.stt_calls == 0
    assert result.changed is False
    assert result.skipped_units == 3


def test_align_marks_its_own_stage_cache_units_per_scene(tmp_path, mock_deps):
    project = _voiced_project_with_three_scenes(tmp_path, mock_deps)
    run_align(project, mock_deps)

    persisted = StageCache(mock_deps.stage_cache.path)
    for scene_id in NARRATIONS:
        # A reloaded cache still knows these units; only the hash decides staleness.
        assert persisted.is_stale(stage_key("voice", scene_id), "not-the-real-hash") is True
        assert persisted.is_stale(stage_key("align", scene_id), "not-the-real-hash") is True
    assert run_align(project, mock_deps).skipped_units == 3
