"""The deterministic next-step panel: it must agree with the runner, always.

The panel's whole claim is that it **cannot be wrong** — it is read from the same
walk `derive_status` performs rather than from a second model of the state
machine. A test that only checked the wording would not defend that claim, so the
load-bearing ones here cross-check the panel against the pipeline itself:

* when the panel says a gate, `run_pipeline` really does block on that gate;
* when the panel says "run", `run_pipeline` really does have work to do;
* when the panel says "done", every gate is stamped and every stage is current.

Everything runs on the mock provider chain: no network, no `ml` extra.
"""

from datetime import UTC, datetime

import pytest

from videomaker.cache import STAGE_ORDER
from videomaker.config import Settings
from videomaker.project import ProjectStore
from videomaker.runner import (
    GATE_BEFORE,
    GateBlocked,
    build_deps,
    run_pipeline,
    stage_cache_for,
    stage_is_current,
)
from videomaker.web.guidance import GATE_ASK, GATE_PATH, next_step


@pytest.fixture
def settings(tmp_path) -> Settings:
    from videomaker.runner import provider_override

    return provider_override(Settings(workspace_dir=tmp_path / "workspace"), "mock")


@pytest.fixture
def store(settings) -> ProjectStore:
    return ProjectStore(settings.workspace_dir)


def _step(store: ProjectStore, project_id: str, **kwargs):
    project = store.load(project_id)
    return next_step(project, stage_cache_for(store, project_id), **kwargs)


def _blocking_gate(settings: Settings, store: ProjectStore, project_id: str) -> str | None:
    """The gate `run_pipeline` actually stops at, or None if it runs out of stages."""
    project = store.load(project_id)
    try:
        run_pipeline(project, build_deps(settings, project_id), until="captions")
    except GateBlocked as blocked:
        return blocked.gate
    return None


def test_a_project_with_no_script_is_the_machines_turn(settings, store):
    project = store.create("how ssds work", "tech_explainer", target_minutes=0.5)

    step = _step(store, project.id)

    assert step.kind == "run"
    assert step.action_method == "post"
    assert step.action_url == f"/projects/{project.id}/advance"
    assert step.gate == ""


def test_a_scripted_project_names_the_gate_the_runner_would_stop_at(settings, store):
    """The claim in one test: the panel's gate *is* the runner's gate."""
    project = store.create("how ssds work", "tech_explainer", target_minutes=0.5)
    run_pipeline(project, build_deps(settings, project.id), until="script")

    step = _step(store, project.id)

    assert step.kind == "review"
    assert step.gate == _blocking_gate(settings, store, project.id) == "script"
    assert step.action_url == f"/projects/{project.id}/{GATE_PATH['script']}"
    assert step.ask == GATE_ASK["script"]


def test_approving_gate_one_moves_the_panel_on_to_the_machine(settings, store):
    project = store.create("how ssds work", "tech_explainer", target_minutes=0.5)
    run_pipeline(project, build_deps(settings, project.id), until="script")

    # Stamped exactly as `POST /approve/script` stamps it.
    project = store.load(project.id)
    project.approvals.script = datetime.now(UTC)
    store.save(project)
    step = _step(store, project.id)

    assert step.kind == "run"
    assert _blocking_gate(settings, store, project.id) == "storyboard"


def test_a_fully_run_project_is_finished_and_says_so(settings, store):
    project = store.create("how ssds work", "tech_explainer", target_minutes=0.5)
    run_pipeline(project, build_deps(settings, project.id), yes=True)

    project = store.load(project.id)
    stage_cache = stage_cache_for(store, project.id)
    assert all(stage_is_current(project, stage_cache, stage) for stage in STAGE_ORDER)
    assert all(getattr(project.approvals, gate) is not None for gate in GATE_BEFORE.values())

    step = _step(store, project.id)

    assert step.kind == "done"
    assert step.action_url == f"/projects/{project.id}/render"


def test_a_run_in_flight_outranks_whatever_is_pending(settings, store):
    """`busy` is the one thing that overrides the walk: nothing to do but wait."""
    project = store.create("how ssds work", "tech_explainer", target_minutes=0.5)
    run_pipeline(project, build_deps(settings, project.id), until="script")

    idle = _step(store, project.id)
    busy = _step(store, project.id, busy=True, running_stage="voice")

    assert idle.kind == "review"
    assert busy.kind == "busy"
    assert not busy.has_action, "a busy panel must not offer a second run"
    assert "speaking every scene" in busy.ask


def test_every_gate_has_a_sentence_written_for_a_human(settings):
    """A gate added later must not fall off the panel — it falls back to GATE_REVIEW."""
    assert set(GATE_ASK) == set(GATE_BEFORE.values())
    assert set(GATE_PATH) == set(GATE_BEFORE.values())


def test_the_tone_is_warm_only_when_it_is_the_humans_turn(settings, store):
    """The palette's one rule, asserted where it is decided rather than in CSS."""
    project = store.create("how ssds work", "tech_explainer", target_minutes=0.5)
    assert _step(store, project.id).tone == "is-run"

    run_pipeline(project, build_deps(settings, project.id), until="script")
    assert _step(store, project.id).tone == "is-review"
