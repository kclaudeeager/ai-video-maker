"""The project dashboard: the stepper, the gate rows and the htmx job partial.

Three properties here are worth more than the rest put together, and each has a
test that fails loudly if it breaks:

* **The polling partial stops polling.** `hx-trigger="every 1500ms"` is present
  while a job is queued or running and *absent* once it reaches `done`, `failed`
  or `blocked`. Get this wrong and a tab left open on a finished project makes
  forty requests a minute until the laptop is closed. Both halves are asserted.
* **The stepper marks exactly what `stage_is_current` reports** (design decision
  3). The expectation is computed from the runner, never written out by hand, so
  a template that hardcodes "the first three are green" cannot pass.
* **`POST /advance` does not run the pipeline in the handler** (design decision
  2). It is driven with the worker deliberately not started, so an inline
  `run_pipeline` would leave the project scripted and the assertion would fail.

Everything runs on the mock provider chain: no network, no `ml` extra.
"""

import re
import threading
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from videomaker.cache import STAGE_ORDER
from videomaker.config import Settings
from videomaker.models import Status
from videomaker.project import ProjectStore
from videomaker.runner import (
    GATE_BEFORE,
    GateBlocked,
    build_deps,
    derive_status,
    run_pipeline,
    stage_cache_for,
    stage_is_current,
)
from videomaker.web.app import create_app

#: Same blunt no-CDN guard the other page tests use.
_ABSOLUTE_URL = re.compile(r"https?://", re.IGNORECASE)

#: The stepper's contract with this test: one `data-stage`/`data-current` pair
#: per stage, in `STAGE_ORDER`.
_STEP = re.compile(r'data-stage="([a-z]+)" data-current="(true|false)"')

#: The gate rows' contract: one `data-gate`/`data-approved` pair per gate.
_GATE = re.compile(r'data-gate="([a-z]+)" data-approved="(true|false)"')

_TERMINAL = ("done", "failed", "blocked")


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(workspace_dir=tmp_path / "workspace")


@pytest.fixture
def app(settings):
    return create_app(settings, providers="mock")


@pytest.fixture
def client(app) -> TestClient:
    """A client that does **not** enter the lifespan, so no worker thread runs."""
    return TestClient(app)


@pytest.fixture
def running_client(app):
    """A client whose lifespan is entered, so submitted jobs really execute."""
    with TestClient(app) as running:
        yield running


@pytest.fixture
def store(app) -> ProjectStore:
    return app.state.store


def _deps(app, project_id):
    return build_deps(app.state.settings, project_id)


def _status_of(store: ProjectStore, project_id: str) -> Status:
    return derive_status(store.load(project_id), stage_cache_for(store, project_id))


def _new(store: ProjectStore, topic: str = "how ssds work"):
    return store.create(topic, "tech_explainer", target_minutes=0.5)


def _current_stages(store: ProjectStore, project_id: str) -> dict[str, bool]:
    """What `stage_is_current` says right now — the stepper's only source of truth."""
    project = store.load(project_id)
    cache = stage_cache_for(store, project_id)
    return {stage: stage_is_current(project, cache, stage) for stage in STAGE_ORDER}


def _finished(app, project_id: str, kind: str, job) -> str:
    """Submit `job`, wait for the worker to finish it, return its terminal state."""
    app.state.jobs.submit(project_id, kind, job)
    assert app.state.jobs.wait_idle(60), "the job never finished"
    return app.state.jobs.state_for(project_id).state


@contextmanager
def _job_running(app, project_id: str):
    """Hold a real job in `running` for the body of the `with`, then let it finish."""
    started = threading.Event()
    release = threading.Event()

    def job(progress) -> None:
        progress.update(stage="voice", progress=0.4, message="speaking scene 1")
        started.set()
        release.wait(30)

    app.state.jobs.submit(project_id, "run", job)
    assert started.wait(30), "the worker never picked the job up"
    try:
        yield
    finally:
        release.set()
        app.state.jobs.wait_idle(30)


# ------------------------------------------------------------------- the page


def test_an_unknown_project_id_is_a_404(client):
    assert client.get("/projects/no-such-project").status_code == 404


def test_the_dashboard_lists_the_seven_stages(client, store):
    project = _new(store)

    body = client.get(f"/projects/{project.id}").text

    assert [stage for stage, _ in _STEP.findall(body)] == list(STAGE_ORDER)


def test_the_dashboard_shows_the_derived_status(app, client, store):
    project = _new(store)
    run_pipeline(project, _deps(app, project.id), until="script")

    body = client.get(f"/projects/{project.id}").text

    assert _status_of(store, project.id) is Status.SCRIPT_READY
    assert 'data-status="script_ready"' in body
    assert project.topic in body


@pytest.mark.parametrize("until", [None, "script", "align", "visuals"])
def test_the_stepper_marks_exactly_the_stages_stage_is_current_reports(
    app, client, store, until
):
    """Decision 3: the marks are `stage_is_current`, never a hardcoded prefix."""
    project = _new(store)
    if until is not None:
        run_pipeline(project, _deps(app, project.id), until=until, yes=True)

    body = client.get(f"/projects/{project.id}").text

    expected = _current_stages(store, project.id)
    assert dict(_STEP.findall(body)) == {
        stage: "true" if current else "false" for stage, current in expected.items()
    }


def test_the_stepper_follows_a_stage_that_goes_stale_under_it(app, client, store):
    """Rewriting a narration un-marks the voiced stages; the page must agree."""
    project = _new(store)
    run_pipeline(project, _deps(app, project.id), until="align", yes=True)
    assert _current_stages(store, project.id)["align"] is True

    project.scenes[0].narration = "Rewritten by hand, so the take no longer matches."
    store.save(project)

    body = client.get(f"/projects/{project.id}").text
    assert _current_stages(store, project.id)["align"] is False
    assert dict(_STEP.findall(body))["align"] == "false"


# ------------------------------------------------------------------ gate rows


def test_the_gate_rows_are_all_pending_on_a_new_project(client, store):
    project = _new(store)

    body = client.get(f"/projects/{project.id}").text

    assert dict(_GATE.findall(body)) == dict.fromkeys(GATE_BEFORE.values(), "false")


def test_an_approved_gate_shows_as_approved(app, client, store):
    """`--yes` through `align` stamps gate 1 and leaves gates 2 and 3 pending."""
    project = _new(store)
    run_pipeline(project, _deps(app, project.id), until="align", yes=True)

    body = client.get(f"/projects/{project.id}").text

    assert store.load(project.id).approvals.script is not None
    assert dict(_GATE.findall(body)) == {
        "script": "true",
        "storyboard": "false",
        "preview": "false",
    }


# ---------------------------------------------------------- the job partial


def test_the_job_partial_is_a_fragment_not_a_whole_page(client, store):
    project = _new(store)

    response = client.get(f"/projects/{project.id}/job")

    assert response.status_code == 200
    assert "<!doctype" not in response.text.lower()
    assert "<html" not in response.text.lower()


def test_the_job_partial_of_an_unknown_project_is_a_404(client):
    assert client.get("/projects/no-such-project/job").status_code == 404


def test_a_project_that_has_never_run_does_not_poll(client, store):
    """Nothing to watch, so no timer: an untouched dashboard makes no requests."""
    project = _new(store)

    response = client.get(f"/projects/{project.id}/job")

    assert response.status_code == 200
    assert "hx-trigger" not in response.text


def test_the_partial_polls_while_the_job_is_queued(client, app, store):
    """The worker is not started here, so the job stays `queued` — still unfinished."""
    project = _new(store)
    app.state.jobs.submit(project.id, "run", lambda progress: None)

    body = client.get(f"/projects/{project.id}/job").text

    assert app.state.jobs.state_for(project.id).state == "queued"
    assert 'hx-trigger="every 1500ms"' in body


def test_the_partial_polls_while_the_job_is_running(running_client, app, store):
    project = _new(store)

    with _job_running(app, project.id):
        body = running_client.get(f"/projects/{project.id}/job").text

        assert app.state.jobs.state_for(project.id).state == "running"
        assert 'hx-trigger="every 1500ms"' in body
        assert "speaking scene 1" in body


@pytest.mark.parametrize(
    ("expected", "job"),
    [
        ("done", lambda progress: None),
        ("failed", lambda progress: (_ for _ in ()).throw(RuntimeError("ffmpeg exploded"))),
        (
            "blocked",
            lambda progress: (_ for _ in ()).throw(GateBlocked("script", Status.SCRIPT_READY)),
        ),
    ],
)
def test_the_partial_stops_polling_at_a_terminal_state(
    running_client, app, store, expected, job
):
    """THE point of this task: a finished tab must stop making requests."""
    project = _new(store)

    assert _finished(app, project.id, "run", job) == expected

    response = running_client.get(f"/projects/{project.id}/job")

    assert expected in _TERMINAL
    assert response.status_code == 200
    assert expected in response.text, "the terminal state is not even shown"
    assert "hx-trigger" not in response.text, f"a {expected} job kept polling"


def test_the_failed_partial_shows_the_error(running_client, app, store):
    project = _new(store)

    def job(progress) -> None:
        raise RuntimeError("ffmpeg exploded")

    assert _finished(app, project.id, "run", job) == "failed"

    assert "ffmpeg exploded" in running_client.get(f"/projects/{project.id}/job").text


def test_the_dashboard_embeds_the_job_partial(client, app, store):
    """The page ships the partial inline, so the first poll is not needed to see it."""
    project = _new(store)
    app.state.jobs.submit(project.id, "run", lambda progress: None)

    body = client.get(f"/projects/{project.id}").text

    assert f'hx-get="/projects/{project.id}/job"' in body
    assert 'hx-trigger="every 1500ms"' in body


# --------------------------------------------------------------- POST /advance


def test_advance_redirects_back_to_the_dashboard(client, store):
    project = _new(store)

    response = client.post(f"/projects/{project.id}/advance", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/projects/{project.id}"


def test_advance_does_not_run_the_pipeline_in_the_handler(client, app, store):
    """Decision 2. The worker is not running, so a queued job cannot mask an inline run."""
    project = _new(store)

    client.post(f"/projects/{project.id}/advance", follow_redirects=False)

    assert store.load(project.id).scenes == []
    assert _status_of(store, project.id) is Status.NEW
    state = app.state.jobs.state_for(project.id)
    assert (state.kind, state.state) == ("run", "queued")


def test_advance_runs_up_to_the_next_gate_and_stops_there(running_client, app, store):
    """A run with no approvals stops at gate 1 — blocked, not failed."""
    project = _new(store)

    running_client.post(f"/projects/{project.id}/advance", follow_redirects=False)
    assert app.state.jobs.wait_idle(120), "the advance job never finished"

    state = app.state.jobs.state_for(project.id)
    assert state.state == "blocked", state.error
    assert state.gate == "script"
    assert _status_of(store, project.id) is Status.SCRIPT_READY


def test_advance_on_an_unknown_project_is_a_404(client):
    response = client.post("/projects/no-such-project/advance", follow_redirects=False)

    assert response.status_code == 404


def test_the_dashboard_page_has_no_external_asset_reference(client, store):
    project = _new(store)

    response = client.get(f"/projects/{project.id}")

    assert response.status_code == 200
    assert not _ABSOLUTE_URL.search(response.text)
