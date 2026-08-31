"""The project list and the create form.

Two rules are load-bearing here and each has a test that fails loudly if it is
broken:

* **Status is derived, never stored** (design decision 3). The list page reads
  `runner.derive_status` for every project, so a project whose script was edited
  under it reports the truth rather than a cached label.
* **A request handler never runs a stage** (design decision 2). `POST /projects`
  enqueues and redirects; `test_creating_does_not_run_the_pipeline_in_the_handler`
  drives it with the worker deliberately not started, so if the handler ever ran
  `run_pipeline` inline the project would come back scripted and the test fails.

Everything runs on the mock provider chain: no network, no `ml` extra.
"""

import re

import pytest
from fastapi.testclient import TestClient

from videomaker.config import Settings
from videomaker.models import Status
from videomaker.project import ProjectStore
from videomaker.providers.errors import ProviderConfigError
from videomaker.providers.mock import MockTTS
from videomaker.runner import derive_status, run_pipeline, stage_cache_for
from videomaker.templates import list_templates
from videomaker.web.app import create_app
from videomaker.web.voices import clear_voice_cache
from videomaker.web.worker import JobQueueFull

#: Same blunt no-CDN guard as `tests/unit/test_web_templates.py`, applied to a
#: real rendered page rather than to the shell alone.
_ABSOLUTE_URL = re.compile(r"https?://", re.IGNORECASE)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(workspace_dir=tmp_path / "workspace")


@pytest.fixture
def app(settings):
    return create_app(settings, providers="mock")


@pytest.fixture
def client(app) -> TestClient:
    """A client that does **not** enter the lifespan, so no worker thread runs.

    That is the point for most tests here: a submitted job stays `queued`, which
    makes the handler's own behaviour observable with nothing racing it.
    """
    return TestClient(app)


@pytest.fixture
def store(app) -> ProjectStore:
    return app.state.store


@pytest.fixture(autouse=True)
def _no_cached_voice_catalogue():
    """`available_voices` memoises a working catalogue; keep that out of other tests."""
    clear_voice_cache()
    yield
    clear_voice_cache()


def _deps(app, project_id):
    from videomaker.runner import build_deps

    return build_deps(app.state.settings, project_id)


def _status_of(store: ProjectStore, project_id: str) -> Status:
    return derive_status(store.load(project_id), stage_cache_for(store, project_id))


def _create(client: TestClient, **fields):
    form = {"topic": "how ssds work", "template": "tech_explainer", "minutes": "0.5"}
    form.update(fields)
    return client.post("/projects", data=form, follow_redirects=False)


# ----------------------------------------------------------------- the list page


def test_the_empty_state_renders(client):
    response = client.get("/")

    assert response.status_code == 200
    assert "no projects" in response.text.lower()


def test_the_list_shows_every_project_with_its_derived_status(app, client, store):
    fresh = store.create("brand new idea", "tech_explainer", target_minutes=0.5)
    scripted = store.create("how ssds work", "tech_explainer", target_minutes=0.5)
    run_pipeline(scripted, _deps(app, scripted.id), until="script")

    body = client.get("/").text

    assert fresh.id in body
    assert scripted.id in body
    assert _status_of(store, scripted.id) is Status.SCRIPT_READY
    assert 'data-status="new"' in body
    assert 'data-status="script_ready"' in body


def test_the_list_reports_the_status_derived_now_not_one_stored_earlier(app, client, store):
    """Decision 3: hand-edit a voiced project's script and the page drops it back.

    This is the case a stored status gets wrong. The project really was voiced;
    then a narration changed under it, so the take on disk no longer matches the
    script and `derive_status` says `script_ready` again. The page must agree.
    """
    project = store.create("how ssds work", "tech_explainer", target_minutes=0.5)
    run_pipeline(project, _deps(app, project.id), until="align", yes=True)
    assert _status_of(store, project.id) is Status.VOICED
    assert 'data-status="voiced"' in client.get("/").text

    project.scenes[0].narration = "Rewritten by hand, so the take no longer matches."
    store.save(project)

    assert _status_of(store, project.id) is Status.SCRIPT_READY
    assert 'data-status="script_ready"' in client.get("/").text


def test_the_template_dropdown_is_populated_from_list_templates(client):
    body = client.get("/").text

    names = list_templates()
    assert names, "the repo ships at least one template"
    for name in names:
        assert f'value="{name}"' in body


def test_the_page_has_no_external_asset_reference(client, store):
    store.create("how ssds work", "tech_explainer", target_minutes=0.5)

    response = client.get("/")
    assert response.status_code == 200
    assert not _ABSOLUTE_URL.search(response.text)


# -------------------------------------------------------------------- creating


def test_creating_redirects_303_to_the_project_and_writes_it_to_disk(client, store):
    response = _create(client, topic="how ssds work")

    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/projects/")
    project_id = location.rsplit("/", 1)[-1]
    assert (store.path_for(project_id) / "project.json").is_file()
    assert store.load(project_id).topic == "how ssds work"


def test_creating_keeps_the_submitted_fields(client, store):
    response = _create(client, topic="how ssds work", minutes="1.5", voice="am_adam")

    project = store.load(response.headers["location"].rsplit("/", 1)[-1])
    assert project.target_minutes == 1.5
    assert project.voice == "am_adam"
    assert project.template == "tech_explainer"


def test_creating_does_not_run_the_pipeline_in_the_handler(app, client, store):
    """Decision 2. The worker is not running, so a queued job cannot mask an inline run."""
    response = _create(client, topic="how ssds work")

    project_id = response.headers["location"].rsplit("/", 1)[-1]
    assert store.load(project_id).scenes == []
    assert _status_of(store, project_id) is Status.NEW
    assert app.state.jobs.state_for(project_id).state == "queued"


def test_creating_enqueues_a_run_job_limited_to_the_script_stage(app, settings):
    """With the worker running the job really executes — and stops after `script`."""
    with TestClient(app) as running:
        response = _create(running, topic="how ssds work")
        project_id = response.headers["location"].rsplit("/", 1)[-1]
        assert app.state.jobs.wait_idle(60), "the run job never finished"

    store = app.state.store
    state = app.state.jobs.state_for(project_id)
    assert state.kind == "run"
    assert state.state == "done", state.error
    # `until="script"`: scripted, and stopped short of voicing.
    assert _status_of(store, project_id) is Status.SCRIPT_READY
    assert store.load(project_id).scenes


def test_two_projects_with_the_same_topic_get_distinct_ids(client, store):
    first = _create(client, topic="how ssds work")
    second = _create(client, topic="how ssds work")

    ids = [r.headers["location"].rsplit("/", 1)[-1] for r in (first, second)]
    assert ids[0] != ids[1]
    assert sorted(store.list_ids()) == sorted(ids)


# ------------------------------------------------------------------- rejections


@pytest.mark.parametrize("topic", ["", "   "])
def test_an_empty_topic_is_rejected_with_a_form_error(client, store, topic):
    response = _create(client, topic=topic)

    assert response.status_code == 422, "a blank topic is a form error, never a 500"
    assert "topic" in response.text.lower()
    assert store.list_ids() == []


def test_an_unknown_template_is_rejected_with_a_form_error(client, store):
    response = _create(client, template="no-such-template")

    assert response.status_code == 422
    assert "template" in response.text.lower()
    assert store.list_ids() == []


def test_a_full_queue_is_reported_rather_than_crashing(app, client, monkeypatch):
    def _full(*args, **kwargs):
        raise JobQueueFull("the job queue is full")

    monkeypatch.setattr(app.state.jobs, "submit", _full)

    response = _create(client, topic="how ssds work")

    assert response.status_code == 503
    # The project itself was still created, so the run can be started from its page.
    assert app.state.store.list_ids()


# ----------------------------------------------------------------- the voice menu


def test_the_voice_field_is_a_select_with_the_default_preselected(client):
    """Kokoro's voice set is fixed and known, so nobody should have to type an id."""
    body = client.get("/").text

    assert '<select id="voice" name="voice">' in body
    assert '<input type="text" id="voice"' not in body
    assert '<optgroup label="American English">' in body
    assert '<option value="af_heart" selected>Heart — female</option>' in body


def test_the_voice_menu_is_grouped_by_language(client, monkeypatch):
    """54 flat options is the thing being fixed; the grouping is the fix."""
    monkeypatch.setattr(MockTTS, "voices", lambda self: ["bm_george", "af_heart", "jf_alpha"])
    clear_voice_cache()

    body = client.get("/").text

    for label in ("American English", "British English", "Japanese"):
        assert f'<optgroup label="{label}">' in body
    assert '<option value="bm_george">George — male</option>' in body


def test_the_form_still_renders_when_the_voice_list_cannot_be_read(client, monkeypatch):
    """A fresh clone has no Kokoro weights. That is a short menu, not a 500."""

    def no_weights(self):
        raise ProviderConfigError("missing Kokoro model files; run `videomaker setup`")

    monkeypatch.setattr(MockTTS, "voices", no_weights)
    clear_voice_cache()

    response = client.get("/")

    assert response.status_code == 200
    assert '<option value="af_heart" selected>Heart — female</option>' in response.text
    assert "videomaker setup" in response.text


def test_a_voice_the_menu_does_not_list_is_still_accepted(client, store):
    """The server-side contract is unchanged: `voice` was never validated."""
    response = _create(client, topic="how ssds work", voice="zz_experimental")

    assert response.status_code == 303
    project = store.load(response.headers["location"].rsplit("/", 1)[-1])
    assert project.voice == "zz_experimental"


def test_a_rejected_form_keeps_an_unlisted_voice_selected(client):
    """Re-rendering at 422 must not silently swap the user's voice for the default."""
    response = _create(client, topic="", voice="zz_experimental")

    assert response.status_code == 422
    assert '<option value="zz_experimental" selected>' in response.text
