"""Gate 1: the script review page, per-scene saving, and the approve action.

**The test that earns this file's keep is `test_a_no_op_save_rewrites_nothing`.**
The page autosaves on every keystroke pause, so `POST /projects/{id}/scenes/{sid}`
is hit constantly with text that has not changed. If that path writes
`project.json` anyway it destroys M1's caching promise in the least visible way
possible: every stage that fingerprints the project would see a fresh file, the
lock would be taken dozens of times a minute against a possibly-running pipeline,
and the user's laptop would re-voice scenes for nothing. So the no-op case is
pinned three ways — the bytes, the mtime **and the inode** of `project.json`
(`ProjectStore.save` is an atomic `os.replace`, so a rewrite always changes the
inode), plus the bytes of `cache/stages.json` and the voice stage still being
current afterwards.

The other properties asserted here:

* **Saving happens under the project lock, and a no-op never takes it.**
  `ProjectStore.save` is spied on and asked, from a second file description,
  whether the `flock` is held — the same question `run_pipeline` would ask from
  the worker thread. `ProjectStore.lock` is spied on separately, because an
  autosave that blocked on a lock a render is holding would hang for minutes.
* **An edit invalidates that scene and nothing else.** The expectation is
  computed from `runner.STAGE_UNITS` rather than written out by hand, so a save
  path that blanket-invalidated a stage could not pass.
* **A browser's CRLF is not an edit.** HTML normalises textarea newlines to
  CRLF on submit, so without normalisation *every* browser autosave would look
  like a change and the no-op guard would never once fire in real use.
* **A blank field is "leave this alone", never "empty this"** — in every
  spelling, because FastAPI cannot tell an empty form value from an absent one.
* **Editing after approval clears the downstream approvals** — M1's
  `clear_stale_approvals` decides that; this file only asserts the UI says so.

Everything runs on the mock provider chain: no network, no `ml` extra.
"""

import fcntl
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from videomaker.config import Settings
from videomaker.project import LOCK_FILE, PROJECT_FILE, ProjectStore
from videomaker.runner import (
    STAGE_UNITS,
    STAGES_RELPATH,
    build_deps,
    run_pipeline,
    stage_cache_for,
    stage_is_current,
    status_key,
)
from videomaker.web.app import create_app

#: The same blunt no-CDN guard the other page tests use.
_ABSOLUTE_URL = re.compile(r"https?://", re.IGNORECASE)

#: One narration textarea per scene, and one visual-query input per scene.
_TEXTAREA = re.compile(r'<textarea[^>]*\bname="narration"')
_QUERY_INPUT = re.compile(r'<input[^>]*\bname="query"')

#: Same `data-gate`/`data-approved` contract the dashboard uses, so the two pages
#: cannot describe the same approval differently.
_GATE = re.compile(r'data-gate="([a-z]+)" data-approved="(true|false)"')

#: What htmx puts on every request it makes. The save endpoint answers a real
#: browser form post with a redirect and an htmx request with the row partial.
_HX = {"HX-Request": "true"}


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(workspace_dir=tmp_path / "workspace")


@pytest.fixture
def app(settings):
    return create_app(settings, providers="mock")


@pytest.fixture
def client(app) -> TestClient:
    """A client that does **not** enter the lifespan, so no worker thread runs.

    That is what lets `test_approving_does_not_run_the_pipeline_in_the_handler`
    mean anything: a job submitted here stays `queued` forever, so an inline
    `run_pipeline` in the handler could not hide behind the worker.
    """
    return TestClient(app)


@pytest.fixture
def store(app) -> ProjectStore:
    return app.state.store


@pytest.fixture
def lock_probe(monkeypatch) -> list[bool]:
    """Records, for every `ProjectStore.save`, whether the project lock was held."""
    saves: list[bool] = []
    original = ProjectStore.save

    def spying_save(self: ProjectStore, project) -> None:
        saves.append(_lock_is_held(self.path_for(project.id) / LOCK_FILE))
        original(self, project)

    monkeypatch.setattr(ProjectStore, "save", spying_save)
    return saves


@pytest.fixture
def locks_taken(monkeypatch) -> list[str]:
    """Every `ProjectStore.lock` the code under test opens.

    A save with nothing to save must not appear here at all: `run_pipeline`
    holds this same `flock` for a whole render, so an autosave that took it
    unconditionally would block a request thread for minutes at a time.
    """
    taken: list[str] = []
    original = ProjectStore.lock

    def spying_lock(self: ProjectStore, project_id: str):
        taken.append(project_id)
        return original(self, project_id)

    monkeypatch.setattr(ProjectStore, "lock", spying_lock)
    return taken


def _lock_is_held(path: Path) -> bool:
    """True when someone holds `flock` on `path`.

    `flock` belongs to the *open file description*, not the process, so a second
    `open()` here conflicts with the store's lock exactly as another process (or
    the worker thread's `run_pipeline`) would.
    """
    if not path.exists():
        return False
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
    return False


def _new(store: ProjectStore, topic: str = "how ssds work"):
    return store.create(topic, "tech_explainer", target_minutes=0.5)


def _scripted(app, store: ProjectStore, *, until: str = "visuals", yes: bool = True):
    """A project run as far as `until`, so there is a real script to review."""
    project = _new(store)
    run_pipeline(project, build_deps(app.state.settings, project.id), until=until, yes=yes)
    return store.load(project.id)


def _units(store: ProjectStore, project_id: str, stage: str) -> dict[str, bool]:
    """Per-unit currency of `stage`, straight from the runner — never hand-written."""
    project = store.load(project_id)
    cache = stage_cache_for(store, project_id)
    return {
        unit.unit: unit.produced and not cache.is_stale(status_key(stage, unit.unit), unit.fingerprint)
        for unit in STAGE_UNITS[stage](project)
    }


def _on_disk(store: ProjectStore, project_id: str) -> tuple[bytes, int, int, bytes]:
    """Everything a save would disturb: project.json's bytes, mtime **and inode**,
    plus the stage cache's bytes. `save()` is an atomic `os.replace`, so a rewrite
    changes the inode even when the bytes and the clock happen to agree."""
    root = store.path_for(project_id)
    project_json = root / PROJECT_FILE
    stat = project_json.stat()
    stages = root / STAGES_RELPATH
    return (
        project_json.read_bytes(),
        stat.st_mtime_ns,
        stat.st_ino,
        stages.read_bytes() if stages.is_file() else b"",
    )


# --------------------------------------------------------------- GET the page


def test_the_script_page_of_an_unknown_project_is_a_404(client):
    assert client.get("/projects/no-such-project/script").status_code == 404


def test_the_script_page_has_one_textarea_and_one_query_input_per_scene(app, client, store):
    project = _scripted(app, store)

    body = client.get(f"/projects/{project.id}/script").text

    assert len(project.scenes) >= 2
    assert len(_TEXTAREA.findall(body)) == len(project.scenes)
    assert len(_QUERY_INPUT.findall(body)) == len(project.scenes)
    for scene in project.scenes:
        assert f'data-scene="{scene.id}"' in body
        assert scene.narration in body


def test_the_script_page_of_a_project_with_no_script_yet_still_renders(client, store):
    project = _new(store)

    response = client.get(f"/projects/{project.id}/script")

    assert response.status_code == 200
    assert _TEXTAREA.findall(response.text) == []
    assert "No script yet" in response.text


def test_the_script_page_offers_the_approve_action(app, client, store):
    project = _scripted(app, store, until="script", yes=False)

    body = client.get(f"/projects/{project.id}/script").text

    assert f'action="/projects/{project.id}/approve/script"' in body
    assert dict(_GATE.findall(body))["script"] == "false"


def test_the_script_page_has_no_external_asset_reference(app, client, store):
    project = _scripted(app, store)

    response = client.get(f"/projects/{project.id}/script")

    assert response.status_code == 200
    assert not _ABSOLUTE_URL.search(response.text)


# ------------------------------------------------------------- POST a scene


def test_editing_a_narration_persists_it(app, client, store):
    project = _scripted(app, store)
    edited = "Rewritten narration for the first scene."

    response = client.post(
        f"/projects/{project.id}/scenes/s01", data={"narration": edited}, headers=_HX
    )

    assert response.status_code == 200
    assert store.load(project.id).scene_by_id("s01").narration == edited
    # Untouched neighbours stay exactly as the script stage wrote them.
    assert store.load(project.id).scene_by_id("s02").narration == project.scene_by_id("s02").narration


def test_the_save_returns_the_scene_row_partial(app, client, store):
    project = _scripted(app, store)

    response = client.post(
        f"/projects/{project.id}/scenes/s01", data={"narration": "New words entirely."}, headers=_HX
    )

    assert response.status_code == 200
    assert "<html" not in response.text.lower()
    assert "<!doctype" not in response.text.lower()
    assert 'data-scene="s01"' in response.text
    assert "New words entirely." in response.text


def test_a_plain_browser_form_post_redirects_back_to_the_page(app, client, store):
    """No JavaScript: a real `<form>` must not leave a bare fragment on screen."""
    project = _scripted(app, store)

    response = client.post(
        f"/projects/{project.id}/scenes/s01",
        data={"narration": "Saved without JavaScript."},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/projects/{project.id}/script#scene-s01"
    assert store.load(project.id).scene_by_id("s01").narration == "Saved without JavaScript."


def test_saving_an_unknown_scene_is_a_404(app, client, store):
    project = _scripted(app, store)

    response = client.post(
        f"/projects/{project.id}/scenes/s99", data={"narration": "nowhere"}, headers=_HX
    )

    assert response.status_code == 404


def test_saving_on_an_unknown_project_is_a_404(client):
    response = client.post(
        "/projects/no-such-project/scenes/s01", data={"narration": "nowhere"}, headers=_HX
    )

    assert response.status_code == 404


@pytest.mark.parametrize("blank", ["", "   ", "\r\n", "\n \t"])
def test_a_blank_field_is_left_alone_rather_than_saved(app, client, store, blank):
    """Autosave fires mid-edit; a momentarily empty box must not wipe the scene.

    Every spelling of "blank" has to behave the same way, including `""` — which
    FastAPI substitutes the `Form` default for, making it indistinguishable from
    a field the form never sent. An endpoint that refused `"  "` but silently
    ignored `""` would be drawing a line it cannot actually see.
    """
    project = _scripted(app, store)
    before = _on_disk(store, project.id)

    response = client.post(
        f"/projects/{project.id}/scenes/s01", data={"narration": blank}, headers=_HX
    )

    assert response.status_code == 200
    assert store.load(project.id).scene_by_id("s01").narration == project.scene_by_id("s01").narration
    assert _on_disk(store, project.id) == before


def test_the_save_goes_through_the_project_lock(app, client, store, lock_probe):
    project = _scripted(app, store)
    # `create()` and the pipeline run have already saved; only the route matters.
    lock_probe.clear()

    client.post(
        f"/projects/{project.id}/scenes/s01", data={"narration": "Written under the lock."}, headers=_HX
    )

    assert lock_probe, "the save never reached ProjectStore.save"
    assert all(lock_probe), "project.json was written without holding the project lock"


# ----------------------------------------------------- THE no-op save guard


def test_a_no_op_save_rewrites_nothing(app, client, store, lock_probe, locks_taken):
    """Re-posting identical text must not touch disk and must not cost a re-voice."""
    project = _scripted(app, store)
    unchanged = project.scene_by_id("s01").narration
    assert stage_is_current(
        store.load(project.id), stage_cache_for(store, project.id), "voice"
    ), "the fixture must start with a current voice stage or this proves nothing"
    lock_probe.clear()
    locks_taken.clear()
    before = _on_disk(store, project.id)

    response = client.post(
        f"/projects/{project.id}/scenes/s01",
        data={"narration": unchanged, "query": project.scene_by_id("s01").visual.query},
        headers=_HX,
    )

    assert response.status_code == 200
    assert _on_disk(store, project.id) == before, "an identical save rewrote project.json"
    assert lock_probe == [], "an identical save called ProjectStore.save"
    assert locks_taken == [], "an identical save took the project lock a render could be holding"
    assert stage_is_current(
        store.load(project.id), stage_cache_for(store, project.id), "voice"
    ), "an identical save invalidated the voice cache"


def test_a_browser_crlf_repost_of_the_same_narration_is_still_a_no_op(app, client, store, lock_probe):
    """HTML form submission turns every newline into CRLF — that is not an edit."""
    project = _scripted(app, store)
    with store.lock(project.id):
        project.scene_by_id("s01").narration = "First line.\nSecond line."
        store.save(project)
    lock_probe.clear()
    before = _on_disk(store, project.id)

    response = client.post(
        f"/projects/{project.id}/scenes/s01",
        data={"narration": "First line.\r\nSecond line.\r\n"},
        headers=_HX,
    )

    assert response.status_code == 200
    assert _on_disk(store, project.id) == before, "a CRLF repost counted as an edit"
    assert lock_probe == []


# ---------------------------------------------------- what an edit invalidates


def test_editing_a_narration_invalidates_only_that_scenes_voice_and_align_units(app, client, store):
    project = _scripted(app, store)
    for stage in ("script", "voice", "align", "visuals"):
        assert all(_units(store, project.id, stage).values()), f"{stage} was not current to begin with"

    client.post(
        f"/projects/{project.id}/scenes/s01",
        data={"narration": "A completely different first scene."},
        headers=_HX,
    )

    others = [scene.id for scene in project.scenes if scene.id != "s01"]
    assert _units(store, project.id, "voice") == {"s01": False, **dict.fromkeys(others, True)}
    assert _units(store, project.id, "align") == {"s01": False, **dict.fromkeys(others, True)}
    # Nothing else moved: the visual for s01 is still the one that was chosen.
    assert all(_units(store, project.id, "visuals").values())
    assert all(_units(store, project.id, "script").values())


def test_editing_a_visual_query_invalidates_only_that_scenes_visual(app, client, store):
    project = _scripted(app, store)

    client.post(
        f"/projects/{project.id}/scenes/s02",
        data={"query": "a close-up of a NAND flash die"},
        headers=_HX,
    )

    saved = store.load(project.id)
    assert saved.scene_by_id("s02").visual.query == "a close-up of a NAND flash die"
    others = [scene.id for scene in project.scenes if scene.id != "s02"]
    assert _units(store, project.id, "visuals") == {"s02": False, **dict.fromkeys(others, True)}
    assert all(_units(store, project.id, "voice").values())
    assert all(_units(store, project.id, "align").values())


# ------------------------------------------------------------------ approval


def test_approving_stamps_the_timestamp_and_enqueues_a_run(app, client, store):
    project = _scripted(app, store, until="script", yes=False)
    assert store.load(project.id).approvals.script is None

    response = client.post(f"/projects/{project.id}/approve/script", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/projects/{project.id}"
    assert store.load(project.id).approvals.script is not None
    state = app.state.jobs.state_for(project.id)
    assert (state.kind, state.state) == ("run", "queued")


def test_approving_does_not_run_the_pipeline_in_the_handler(app, client, store):
    """Decision 2. The worker is not running, so an inline run could not hide."""
    project = _scripted(app, store, until="script", yes=False)

    client.post(f"/projects/{project.id}/approve/script", follow_redirects=False)

    assert all(scene.audio_path is None for scene in store.load(project.id).scenes)


def test_approving_an_already_approved_gate_is_idempotent(app, client, store):
    project = _scripted(app, store, until="script", yes=False)
    client.post(f"/projects/{project.id}/approve/script", follow_redirects=False)
    stamped = store.load(project.id).approvals.script

    response = client.post(f"/projects/{project.id}/approve/script", follow_redirects=False)

    assert response.status_code == 303
    assert store.load(project.id).approvals.script == stamped


def test_approving_an_unknown_project_is_a_404(client):
    assert client.post("/projects/no-such-project/approve/script").status_code == 404


def test_the_page_shows_the_gate_as_approved_once_it_is(app, client, store):
    project = _scripted(app, store, until="script", yes=False)
    client.post(f"/projects/{project.id}/approve/script", follow_redirects=False)

    body = client.get(f"/projects/{project.id}/script").text

    assert dict(_GATE.findall(body))["script"] == "true"


# ------------------------------------------- editing clears downstream approvals


def test_editing_after_approval_clears_the_downstream_approvals(app, client, store):
    """M1's `clear_stale_approvals` decides this; the UI must say it out loud."""
    project = _scripted(app, store, until="captions", yes=True)
    assert store.load(project.id).approvals.storyboard is not None

    response = client.post(
        f"/projects/{project.id}/scenes/s01",
        data={"narration": "Rewritten well after the storyboard was signed off."},
        headers=_HX,
    )

    saved = store.load(project.id)
    assert saved.approvals.storyboard is None, "a rewritten narration sailed through gate 2"
    # Gate 1 guards `voice`, whose only upstream stage is `script` — still current.
    assert saved.approvals.script is not None

    # The UI surfaces it in the response itself (an out-of-band gate update) ...
    assert dict(_GATE.findall(response.text))["storyboard"] == "false"
    assert "hx-swap-oob" in response.text
    # ... on a reload of the review page ...
    assert dict(_GATE.findall(client.get(f"/projects/{project.id}/script").text)) == {
        "script": "true",
        "storyboard": "false",
        "preview": "false",
    }
    # ... and on the dashboard, which must not disagree with it.
    assert dict(_GATE.findall(client.get(f"/projects/{project.id}").text))["storyboard"] == "false"
