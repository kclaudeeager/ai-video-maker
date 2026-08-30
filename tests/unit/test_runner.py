"""Derived status, the three review gates, and stage orchestration.

Two tests here are the reason status is derived rather than stored:
`test_editing_a_narration_after_voicing_reports_script_ready` proves a project
cannot claim to be voiced once its script moved underneath it, and
`test_editing_an_earlier_stage_clears_the_downstream_approval` proves a stale
approval cannot smuggle unreviewed content through the gate that exists to
catch it. Everything runs on the mock chain: no network, no `ml` extra.
"""

import pytest

from videomaker import runner as runner_module
from videomaker.cache import ResponseCache, StageCache
from videomaker.config import Settings
from videomaker.models import Status
from videomaker.pipeline.base import StageDeps
from videomaker.project import ProjectStore
from videomaker.providers.ratelimit import QuotaTracker
from videomaker.runner import (
    PROVIDER_KINDS,
    GateBlocked,
    StageFailed,
    clear_stale_approvals,
    derive_status,
    provider_override,
    run_pipeline,
)

REWRITE = "Scene two, rewritten by hand after the take was already voiced."


@pytest.fixture
def deps(tmp_path):
    settings = Settings(
        workspace_dir=tmp_path / "workspace",
        provider_chains={kind: ["mock"] for kind in PROVIDER_KINDS},
    )
    store = ProjectStore(settings.workspace_dir)
    return StageDeps(
        settings=settings,
        store=store,
        stage_cache=StageCache(tmp_path / "stages.json"),
        response_cache=ResponseCache(tmp_path / "responses"),
        quota=QuotaTracker(tmp_path / "quota.json"),
    )


@pytest.fixture
def project(deps):
    return deps.store.create("how ssds work", "tech_explainer", target_minutes=0.5)


# ------------------------------------------------------------------ derived status


def test_a_project_with_no_script_is_new(project, deps):
    assert derive_status(project, deps.stage_cache) is Status.NEW


def test_a_scripted_project_is_script_ready(project, deps):
    run_pipeline(project, deps, until="script")

    assert project.scenes
    assert derive_status(project, deps.stage_cache) is Status.SCRIPT_READY


def test_a_voiced_and_aligned_project_is_voiced(project, deps):
    run_pipeline(project, deps, until="align", yes=True)

    assert all(scene.words for scene in project.scenes)
    assert derive_status(project, deps.stage_cache) is Status.VOICED


def test_a_project_with_visuals_is_storyboard_ready(project, deps):
    run_pipeline(project, deps, until="visuals", yes=True)

    assert derive_status(project, deps.stage_cache) is Status.STORYBOARD_READY


def test_editing_a_narration_after_voicing_reports_script_ready(project, deps):
    run_pipeline(project, deps, until="align", yes=True)
    assert derive_status(project, deps.stage_cache) is Status.VOICED

    project.scene_by_id("s02").narration = REWRITE

    assert derive_status(project, deps.stage_cache) is Status.SCRIPT_READY


def test_editing_a_visual_query_after_the_storyboard_reports_voiced(project, deps):
    run_pipeline(project, deps, until="visuals", yes=True)

    project.scene_by_id("s02").visual.query = "a different shot entirely"

    assert derive_status(project, deps.stage_cache) is Status.VOICED


def test_an_unapproved_gate_holds_the_status_back(project, deps):
    """Finished work behind a closed gate is not progress: the gate decides."""
    run_pipeline(project, deps, until="visuals", yes=True)
    project.approvals.script = None

    assert derive_status(project, deps.stage_cache) is Status.SCRIPT_READY


def test_deleting_a_stage_output_reopens_that_stage(project, deps):
    """Hand-editing `project.json` can remove an output; status must notice."""
    run_pipeline(project, deps, until="align", yes=True)

    project.scene_by_id("s02").words = []

    assert derive_status(project, deps.stage_cache) is Status.SCRIPT_READY


def test_status_stops_at_the_first_unapproved_gate(project, deps):
    run_pipeline(project, deps, until="script")
    assert project.approvals.script is None

    assert derive_status(project, deps.stage_cache) is Status.SCRIPT_READY


# ------------------------------------------------------------------------- gates


def test_run_stops_at_the_script_gate_without_yes(project, deps):
    with pytest.raises(GateBlocked) as caught:
        run_pipeline(project, deps, until="voice")

    assert caught.value.gate == "script"
    assert caught.value.status is Status.SCRIPT_READY
    assert caught.value.review  # tells the human what to look at
    assert project.approvals.script is None
    assert all(scene.audio_path is None for scene in project.scenes)


def test_run_stops_at_the_storyboard_gate_without_yes(project, deps):
    run_pipeline(project, deps, until="visuals", yes=True)

    with pytest.raises(GateBlocked) as caught:
        run_pipeline(project, deps, until="captions")

    assert caught.value.gate == "storyboard"
    assert caught.value.status is Status.STORYBOARD_READY


def test_yes_stamps_the_approval_it_passes(project, deps):
    run_pipeline(project, deps, until="voice", yes=True)

    assert project.approvals.script is not None
    assert project.approvals.storyboard is None
    # Stamped on the project, not just in memory.
    assert deps.store.load(project.id).approvals.script == project.approvals.script


def test_editing_an_earlier_stage_clears_the_downstream_approval(project, deps):
    run_pipeline(project, deps, until="captions", yes=True)
    assert project.approvals.storyboard is not None
    approved_script_at = project.approvals.script

    project.scene_by_id("s02").narration = REWRITE

    assert clear_stale_approvals(project, deps.stage_cache) is True
    assert project.approvals.storyboard is None
    # The human wrote this edit themselves; gate 1 does not re-open for it.
    assert project.approvals.script == approved_script_at
    assert derive_status(project, deps.stage_cache) is Status.SCRIPT_READY


def test_run_clears_stale_approvals_before_it_runs_anything(project, deps):
    run_pipeline(project, deps, until="captions", yes=True)
    project.scene_by_id("s02").narration = REWRITE

    # Gate 1 is still approved, so re-voicing is allowed without --yes...
    run_pipeline(project, deps, until="align")

    # ...but the storyboard the edit invalidated must be reviewed again.
    assert project.approvals.storyboard is None
    with pytest.raises(GateBlocked) as caught:
        run_pipeline(project, deps, until="captions")
    assert caught.value.gate == "storyboard"


def test_nothing_is_cleared_when_every_stage_is_current(project, deps):
    run_pipeline(project, deps, until="captions", yes=True)

    assert clear_stale_approvals(project, deps.stage_cache) is False
    assert project.approvals.storyboard is not None


# ------------------------------------------------------------------ orchestration


def test_until_runs_the_stages_up_to_and_including_that_stage(project, deps):
    seen = []
    run_pipeline(project, deps, until="voice", yes=True, on_stage=lambda s, _r: seen.append(s))

    assert seen == ["script", "voice"]


def test_an_unknown_until_stage_is_rejected(project, deps):
    with pytest.raises(ValueError, match="nonsense"):
        run_pipeline(project, deps, until="nonsense")


def test_a_second_run_repeats_no_work(project, deps):
    run_pipeline(project, deps, until="visuals", yes=True)

    results = {}
    run_pipeline(project, deps, until="visuals", on_stage=lambda s, r: results.update({s: r}))

    assert [stage for stage, result in results.items() if result.changed] == []


def test_a_failing_stage_is_reported_with_its_name(project, deps, monkeypatch):
    def boom(_project, _deps):
        raise RuntimeError("kokoro fell over")

    monkeypatch.setitem(runner_module.STAGE_RUNNERS, "voice", boom)

    with pytest.raises(StageFailed) as caught:
        run_pipeline(project, deps, until="voice", yes=True)

    assert caught.value.stage == "voice"
    assert "kokoro fell over" in str(caught.value)


def test_provider_override_replaces_every_chain():
    overridden = provider_override(Settings(), "mock")

    assert set(overridden.provider_chains) >= set(PROVIDER_KINDS)
    assert all(chain == ["mock"] for chain in overridden.provider_chains.values())


def test_provider_override_covers_kinds_the_settings_never_mention():
    """One kind left pointing at a real provider would reach for the network."""
    partial = Settings(provider_chains={"tts": ["kokoro"], "uploader": ["youtube"]})

    overridden = provider_override(partial, "mock")

    assert set(overridden.provider_chains) == {*PROVIDER_KINDS, "uploader"}
    assert all(chain == ["mock"] for chain in overridden.provider_chains.values())
