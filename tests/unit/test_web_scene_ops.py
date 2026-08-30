"""Splitting, merging, reordering and deleting scenes from the gate 1 page.

Three things this file pins that the pure-function tests cannot:

* **`POST /projects/{id}/scenes/reorder` must not be swallowed by
  `POST /projects/{id}/scenes/{scene_id}`.** Starlette matches routes in
  registration order, so with the obvious ordering `reorder` arrives as a *scene
  id*, `_scene_or_404` fires, and the whole feature answers 404 forever. It is
  invisible in review and obvious in a test.
* **The structural operations still work with JavaScript off.** They are real
  `<form method="post">` submissions that answer 303; htmx gets `HX-Redirect`
  instead of a fragment, because half the page has moved and swapping one row
  would be a lie.
* **A reorder costs nothing.** The web path is where a stray `store.save` of a
  renumbered project would sneak in, so the per-scene units are asserted still
  current and the scene folders still byte-for-byte (and inode-for-inode) the
  ones the pipeline wrote.

Everything runs on the mock provider chain: no network, no `ml` extra.
"""

import fcntl
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from videomaker.config import Settings
from videomaker.project import ProjectStore
from videomaker.runner import (
    STAGE_UNITS,
    build_deps,
    run_pipeline,
    stage_cache_for,
    status_key,
)
from videomaker.web.app import create_app

#: What htmx puts on every request it makes.
_HX = {"HX-Request": "true"}


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(workspace_dir=tmp_path / "workspace")


@pytest.fixture
def app(settings):
    return create_app(settings, providers="mock")


@pytest.fixture
def client(app) -> TestClient:
    """No lifespan, so no worker thread: nothing can run a stage behind our back."""
    return TestClient(app)


@pytest.fixture
def store(app) -> ProjectStore:
    return app.state.store


def _scripted(app, store: ProjectStore, *, until: str = "visuals"):
    """A project run as far as `until`, so there are real scenes to operate on."""
    project = store.create("how ssds work", "tech_explainer", target_minutes=0.5)
    run_pipeline(project, build_deps(app.state.settings, project.id), until=until, yes=True)
    return store.load(project.id)


def _ids(store: ProjectStore, project_id: str) -> list[str]:
    return [scene.id for scene in store.load(project_id).scenes]


def _units(store: ProjectStore, project_id: str, stage: str) -> dict[str, bool]:
    """Per-unit currency straight from the runner — never hand-written."""
    project = store.load(project_id)
    cache = stage_cache_for(store, project_id)
    return {
        unit.unit: unit.produced
        and not cache.is_stale(status_key(stage, unit.unit), unit.fingerprint)
        for unit in STAGE_UNITS[stage](project)
    }


def _scene_files(store: ProjectStore, project_id: str) -> dict[str, tuple[bytes, int]]:
    """Every per-scene artefact: bytes and inode, so an atomic rewrite shows up."""
    root = store.path_for(project_id) / "scenes"
    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_ino)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _approve(store: ProjectStore, project_id: str, *gates: str) -> None:
    with store.lock(project_id):
        project = store.load(project_id)
        for gate in gates:
            setattr(project.approvals, gate, datetime.now(UTC))
        store.save(project)


# ------------------------------------------------------------------- the page


def test_the_page_offers_every_scene_operation_as_a_real_form(app, client, store):
    project = _scripted(app, store)
    assert len(project.scenes) >= 2

    body = client.get(f"/projects/{project.id}/script").text

    for scene in project.scenes:
        assert f'action="/projects/{project.id}/scenes/{scene.id}/split"' in body
        assert f'action="/projects/{project.id}/scenes/{scene.id}/delete"' in body
    assert f'action="/projects/{project.id}/scenes/reorder"' in body
    assert f'action="/projects/{project.id}/scenes/{project.scenes[0].id}/merge"' in body
    # No JavaScript required: every one of them is a plain POST form.
    assert 'name="at_word"' in body
    assert 'name="order"' in body


# ----------------------------------------------------------------------- split


def test_splitting_a_scene_adds_one_and_redirects_back(app, client, store):
    project = _scripted(app, store)
    original = project.scene_by_id("s01").narration
    at_word = 2

    response = client.post(
        f"/projects/{project.id}/scenes/s01/split",
        data={"at_word": at_word},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/projects/{project.id}/script")
    after = store.load(project.id)
    assert len(after.scenes) == len(project.scenes) + 1
    head = after.scenes[0]
    tail = after.scenes[1]
    assert head.id == "s01"
    assert tail.id not in {scene.id for scene in project.scenes}
    # Word for word, in order: nothing is lost at the cut and nothing is added.
    assert head.narration.split() + tail.narration.split() == original.split()


@pytest.mark.parametrize("at_word", [0, 9999])
def test_splitting_at_an_impossible_word_is_a_400(app, client, store, at_word):
    project = _scripted(app, store)

    response = client.post(
        f"/projects/{project.id}/scenes/s01/split", data={"at_word": at_word}
    )

    assert response.status_code == 400
    assert _ids(store, project.id) == [scene.id for scene in project.scenes]


def test_splitting_invalidates_the_original_scene_only(app, client, store):
    project = _scripted(app, store)
    assert all(_units(store, project.id, "voice").values())

    client.post(f"/projects/{project.id}/scenes/s01/split", data={"at_word": 2})

    voice = _units(store, project.id, "voice")
    assert voice["s01"] is False
    assert all(current for sid, current in voice.items() if sid in {"s02", "s03"})


def test_splitting_clears_the_approvals_that_depend_on_the_old_script(app, client, store):
    project = _scripted(app, store)
    _approve(store, project.id, "script", "storyboard", "preview")

    client.post(f"/projects/{project.id}/scenes/s01/split", data={"at_word": 2})

    approvals = store.load(project.id).approvals
    assert approvals.storyboard is None
    assert approvals.preview is None


# ----------------------------------------------------------------------- merge


def test_merging_defaults_to_the_following_scene(app, client, store):
    project = _scripted(app, store)
    joined = f"{project.scenes[0].narration} {project.scenes[1].narration}"

    response = client.post(
        f"/projects/{project.id}/scenes/s01/merge", follow_redirects=False
    )

    assert response.status_code == 303
    after = store.load(project.id)
    assert [scene.id for scene in after.scenes] == [
        scene.id for scene in project.scenes if scene.id != "s02"
    ]
    assert after.scene_by_id("s01").narration == joined


def test_merging_takes_an_explicit_second_id(app, client, store):
    project = _scripted(app, store)
    assert len(project.scenes) >= 3

    client.post(f"/projects/{project.id}/scenes/s01/merge", data={"second_id": "s03"})

    assert "s03" not in _ids(store, project.id)
    assert not (store.path_for(project.id) / "scenes" / "s03").exists()


def test_merging_the_last_scene_with_nothing_is_a_400(app, client, store):
    project = _scripted(app, store)
    last = project.scenes[-1].id

    response = client.post(f"/projects/{project.id}/scenes/{last}/merge")

    assert response.status_code == 400
    assert _ids(store, project.id) == [scene.id for scene in project.scenes]


# ---------------------------------------------------------------------- delete


def test_deleting_a_scene_removes_it_and_its_artifacts(app, client, store):
    project = _scripted(app, store)

    response = client.post(
        f"/projects/{project.id}/scenes/s02/delete", follow_redirects=False
    )

    assert response.status_code == 303
    assert "s02" not in _ids(store, project.id)
    assert not (store.path_for(project.id) / "scenes" / "s02").exists()
    assert (store.path_for(project.id) / "scenes" / "s01").exists()


# --------------------------------------------------------------------- reorder


def test_the_reorder_route_is_not_swallowed_by_the_save_route(app, client, store):
    """`/scenes/reorder` registered after `/scenes/{scene_id}` would 404 forever."""
    project = _scripted(app, store)
    reversed_ids = [scene.id for scene in reversed(project.scenes)]

    response = client.post(
        f"/projects/{project.id}/scenes/reorder",
        data={"order": reversed_ids},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert _ids(store, project.id) == reversed_ids


def test_reordering_re_encodes_nothing_that_belongs_to_a_scene(app, client, store):
    """Decision 5 over HTTP: a move keeps every per-scene unit current, on disk."""
    project = _scripted(app, store)
    for stage in ("voice", "align", "visuals"):
        assert all(_units(store, project.id, stage).values()), stage
    before = _scene_files(store, project.id)
    assert before, "the fixture must have written per-scene artefacts"
    reversed_ids = [scene.id for scene in reversed(project.scenes)]

    client.post(
        f"/projects/{project.id}/scenes/reorder",
        data={"order": reversed_ids},
    )

    assert _ids(store, project.id) == reversed_ids
    for stage in ("voice", "align", "visuals"):
        assert all(_units(store, project.id, stage).values()), stage
    assert _scene_files(store, project.id) == before


def test_reordering_with_an_id_set_that_is_not_the_project_is_a_400(app, client, store):
    project = _scripted(app, store)

    response = client.post(
        f"/projects/{project.id}/scenes/reorder", data={"order": ["s01", "s01"]}
    )

    assert response.status_code == 400
    assert _ids(store, project.id) == [scene.id for scene in project.scenes]


# ------------------------------------------------------- shared route behaviour


@pytest.mark.parametrize(
    ("path", "data"),
    [
        ("/scenes/s01/split", {"at_word": 2}),
        ("/scenes/s01/merge", {}),
        ("/scenes/s01/delete", {}),
        ("/scenes/reorder", {"order": "s01"}),
    ],
)
def test_an_unknown_project_is_a_404(client, path, data):
    assert client.post(f"/projects/no-such-project{path}", data=data).status_code == 404


@pytest.mark.parametrize(
    ("path", "data"),
    [
        ("/scenes/s99/split", {"at_word": 2}),
        ("/scenes/s99/merge", {}),
        ("/scenes/s99/delete", {}),
    ],
)
def test_an_unknown_scene_is_a_404(app, client, store, path, data):
    project = _scripted(app, store)

    assert client.post(f"/projects/{project.id}{path}", data=data).status_code == 404


def test_htmx_is_told_to_navigate_rather_than_swap_a_fragment(app, client, store):
    """Half the page moves, so a partial swap would leave the list lying."""
    project = _scripted(app, store)

    response = client.post(
        f"/projects/{project.id}/scenes/s02/delete", headers=_HX, follow_redirects=False
    )

    assert response.status_code == 204
    assert response.headers["HX-Redirect"].startswith(f"/projects/{project.id}/script")
    assert response.text == ""


def test_a_structural_edit_is_written_under_the_project_lock(app, client, store, monkeypatch):
    """`run_pipeline` holds this same flock for a whole render."""
    held: list[bool] = []
    original = ProjectStore.save

    def spying_save(self: ProjectStore, project) -> None:
        held.append(_lock_is_held(self.path_for(project.id) / ".lock"))
        original(self, project)

    project = _scripted(app, store)
    monkeypatch.setattr(ProjectStore, "save", spying_save)

    client.post(f"/projects/{project.id}/scenes/s02/delete")

    assert held, "the delete never reached ProjectStore.save"
    assert all(held), "project.json was written without holding the project lock"


def _lock_is_held(path: Path) -> bool:
    if not path.exists():
        return False
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
    return False
