"""The folder tree on the list page, and the one control that files a project.

The list page was flat, and with ten projects on the owner's machine it had stopped
being a list and become a wall. Folders group it — but a folder here is a **label**
on the project, not a directory: `web/routes/projects.py` builds the tree at render
time from `Project.folder`, and `workspace/projects/` stays exactly as flat as it
always was. See `tests/unit/test_folders.py` for why that boundary is worth guarding.

Two contracts carry over from every other write in this app and are asserted here:

* **Compare first, lock second.** Re-filing a project into the folder it is already
  in must not take the `flock` a render could be holding for twenty minutes.
  `test_a_no_op_move_takes_no_lock_and_writes_nothing` asserts `locks_taken == []`,
  which is the assertion M2 Task 9's mutant 5 showed a "nothing was written" check
  alone does not make.
* **It works with JavaScript off.** The tree is native `<details>` — the same
  progressive disclosure gate 2 uses — and the move control is a plain
  `<form method="post">` ending in a 303. No htmx is involved in either.
"""

import re

import pytest
from fastapi.testclient import TestClient

from videomaker.config import Settings
from videomaker.project import ProjectStore
from videomaker.web.app import create_app
from videomaker.web.voices import clear_voice_cache


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(workspace_dir=tmp_path / "workspace")


@pytest.fixture
def app(settings):
    return create_app(settings, providers="mock")


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture
def store(app) -> ProjectStore:
    return app.state.store


@pytest.fixture(autouse=True)
def _no_cached_voice_catalogue():
    clear_voice_cache()
    yield
    clear_voice_cache()


@pytest.fixture
def locks_taken(monkeypatch) -> list[str]:
    """Every `ProjectStore.lock` the code under test opens.

    A move with nothing to change must not appear here at all.
    """
    taken: list[str] = []
    original = ProjectStore.lock

    def spying_lock(self: ProjectStore, project_id: str):
        taken.append(project_id)
        return original(self, project_id)

    monkeypatch.setattr(ProjectStore, "lock", spying_lock)
    return taken


def _make(store: ProjectStore, topic: str, folder: str = ""):
    return store.create(topic, "tech_explainer", folder=folder)


def _on_disk(store: ProjectStore, project_id: str) -> str:
    return (store.path_for(project_id) / "project.json").read_text()


def _folders_in(body: str) -> list[str]:
    return re.findall(r'data-folder="([^"]*)"', body)


def _details_for(body: str, label: str) -> str:
    """What one folder's `<details>` contains, descendants included.

    Nesting means the matching `</details>` cannot be found by string search, so the
    block is bounded by the next folder that is *not* a descendant of this one —
    which is exactly the same thing for a tree rendered outside-in.
    """
    after = body.split(f'data-folder="{label}"', 1)[1]
    for found in re.finditer(r'data-folder="([^"]*)"', after):
        if not found.group(1).startswith(f"{label}/"):
            return after[: found.start()]
    return after


# ------------------------------------------------------------------- the tree


def test_a_project_with_no_folder_stays_at_the_root_of_the_list(client, store):
    _make(store, "how ssds work")

    body = client.get("/").text

    assert 'data-project="how-ssds-work"' in body
    assert _folders_in(body) == [], "a flat workspace must draw no folder chrome at all"


def test_the_list_groups_projects_under_their_folder(client, store):
    _make(store, "how ssds work", folder="tech")
    _make(store, "my holiday", folder="personal")
    _make(store, "loose one")

    body = client.get("/").text

    assert sorted(_folders_in(body)) == ["personal", "tech"]
    assert 'data-project="how-ssds-work"' in _details_for(body, "tech")
    assert 'data-project="my-holiday"' in _details_for(body, "personal")


def test_the_tree_nests_several_levels(client, store):
    _make(store, "deep one", folder="tech/office-basics/word")

    body = client.get("/").text

    # Every ancestor is a folder in its own right, in outside-in order, and the
    # project sits inside the innermost one.
    assert _folders_in(body) == ["tech", "tech/office-basics", "tech/office-basics/word"]
    assert 'data-project="deep-one"' in _details_for(body, "tech/office-basics/word")


def test_a_parent_folder_holds_both_its_own_projects_and_its_children(client, store):
    _make(store, "parent project", folder="tech")
    _make(store, "child project", folder="tech/office-basics")

    body = client.get("/").text
    tech = _details_for(body, "tech")

    assert 'data-project="parent-project"' in tech
    assert 'data-project="child-project"' in tech
    assert 'data-folder="tech/office-basics"' in tech


def test_the_folder_is_a_native_details_so_the_tree_works_with_javascript_off(client, store):
    _make(store, "how ssds work", folder="tech")

    body = client.get("/").text

    assert re.search(r"<details[^>]*data-folder=\"tech\"", body), "a folder must be a <details>"
    chunk = _details_for(body, "tech")
    assert "<summary" in chunk
    # The tree is disclosure, not behaviour: nothing in it may depend on htmx.
    assert "hx-" not in chunk.split("</details>", 1)[0], "the tree must not need htmx"


def test_the_folder_count_says_how_many_projects_are_beneath_it(client, store):
    """A `data-*` pair: the machine-readable count and the words must agree."""
    _make(store, "one", folder="tech")
    _make(store, "two", folder="tech/office-basics")
    _make(store, "three", folder="tech/office-basics")
    _make(store, "elsewhere", folder="personal")

    body = client.get("/").text

    assert 'data-folder-count="3">3 projects<' in _details_for(body, "tech")
    assert 'data-folder-count="2">2 projects<' in _details_for(body, "tech/office-basics")
    assert 'data-folder-count="1">1 project<' in _details_for(body, "personal")


def test_the_list_says_a_folder_needs_no_deleting(client, store):
    """There is no folder object, so there is no delete button. Nobody should have to
    discover that by hunting for one."""
    _make(store, "how ssds work", folder="tech")

    body = client.get("/").text.lower()

    assert "nothing to delete" in body
    assert "label" in body


def test_an_empty_folder_simply_stops_being_rendered(client, store):
    project = _make(store, "how ssds work", folder="tech")
    assert "tech" in _folders_in(client.get("/").text)

    client.post(f"/projects/{project.id}/folder", data={"folder": "", "back": "list"})

    assert _folders_in(client.get("/").text) == []


# -------------------------------------------------------------------- moving


def test_moving_from_the_list_files_the_project_and_returns_to_the_list(client, store):
    project = _make(store, "how ssds work")

    response = client.post(
        f"/projects/{project.id}/folder",
        data={"folder": "tech/office-basics", "back": "list"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert store.load(project.id).folder == "tech/office-basics"


def test_moving_from_the_project_page_returns_to_that_page(client, store):
    project = _make(store, "how ssds work")

    response = client.post(
        f"/projects/{project.id}/folder",
        data={"folder": "tech"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/projects/{project.id}"
    assert store.load(project.id).folder == "tech"


def test_the_label_is_normalised_before_it_is_stored(client, store):
    project = _make(store, "how ssds work")

    client.post(f"/projects/{project.id}/folder", data={"folder": "  tech / office-basics "})

    assert store.load(project.id).folder == "tech/office-basics"


def test_an_empty_label_moves_the_project_back_to_the_root(client, store):
    project = _make(store, "how ssds work", folder="tech")

    client.post(f"/projects/{project.id}/folder", data={"folder": ""})

    assert store.load(project.id).folder == ""


def test_a_no_op_move_takes_no_lock_and_writes_nothing(client, store, locks_taken):
    """Compare first, lock second. Re-filing a project where it already is must not
    queue behind a render for a `flock` it does not need."""
    project = _make(store, "how ssds work", folder="tech")
    before = _on_disk(store, project.id)
    locks_taken.clear()

    response = client.post(
        f"/projects/{project.id}/folder", data={"folder": "tech"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert _on_disk(store, project.id) == before
    assert locks_taken == [], "a no-op move took the project lock a render could be holding"


def test_a_move_that_only_differs_by_whitespace_is_still_a_no_op(client, store, locks_taken):
    """The no-op guard compares the *normalised* label, or the form's own round trip
    (`value="tech"` typed back with a stray space) would take the lock every time."""
    project = _make(store, "how ssds work", folder="tech/office-basics")
    locks_taken.clear()

    client.post(f"/projects/{project.id}/folder", data={"folder": " tech / office-basics "})

    assert locks_taken == []


def test_a_real_move_does_take_the_lock(client, store, locks_taken):
    """The guard above is only meaningful if the lock is taken when there is work."""
    project = _make(store, "how ssds work")
    locks_taken.clear()

    client.post(f"/projects/{project.id}/folder", data={"folder": "tech"})

    assert locks_taken == [project.id]


@pytest.mark.parametrize("label", ["../etc", "/tech", "tech\\office", "tech//office"])
def test_a_label_that_could_be_read_as_a_path_is_refused(client, store, label):
    project = _make(store, "how ssds work", folder="tech")
    before = _on_disk(store, project.id)

    response = client.post(f"/projects/{project.id}/folder", data={"folder": label})

    assert response.status_code == 422
    assert _on_disk(store, project.id) == before
    assert store.load(project.id).folder == "tech"


def test_a_refused_move_from_the_list_re_renders_the_list_with_the_reason(client, store):
    project = _make(store, "how ssds work")

    response = client.post(
        f"/projects/{project.id}/folder", data={"folder": "../etc", "back": "list"}
    )

    assert response.status_code == 422
    assert 'data-project="how-ssds-work"' in response.text
    assert "role=\"alert\"" in response.text


def test_a_refused_move_from_the_project_page_re_renders_that_page_with_the_reason(client, store):
    project = _make(store, "how ssds work")

    response = client.post(f"/projects/{project.id}/folder", data={"folder": "../etc"})

    assert response.status_code == 422
    assert "how ssds work" in response.text
    assert "role=\"alert\"" in response.text


def test_a_refused_move_takes_no_lock(client, store, locks_taken):
    project = _make(store, "how ssds work")
    locks_taken.clear()

    client.post(f"/projects/{project.id}/folder", data={"folder": "../etc"})

    assert locks_taken == []


def test_moving_a_project_that_does_not_exist_is_a_404(client):
    response = client.post("/projects/nope/folder", data={"folder": "tech"})

    assert response.status_code == 404


# ------------------------------------------------------------- the move control


def test_every_row_carries_a_plain_move_form(client, store):
    project = _make(store, "how ssds work", folder="tech")

    body = client.get("/").text

    assert f'action="/projects/{project.id}/folder"' in body
    assert 'method="post"' in body


def test_the_project_page_offers_the_same_control_and_shows_where_it_is_filed(client, store):
    project = _make(store, "how ssds work", folder="tech/office-basics")

    body = client.get(f"/projects/{project.id}").text

    assert f'action="/projects/{project.id}/folder"' in body
    assert 'value="tech/office-basics"' in body


def test_the_move_control_offers_the_folders_already_in_use(client, store):
    """Typing a new label creates a folder, so the field is a free-text input — but it
    suggests what exists, or the tree would sprout typo-siblings."""
    _make(store, "one", folder="tech/office-basics")
    _make(store, "two", folder="personal")
    project = _make(store, "three")

    for page in ("/", f"/projects/{project.id}"):
        body = client.get(page).text
        assert "<datalist" in body, page
        for label in ("tech", "tech/office-basics", "personal"):
            assert f'<option value="{label}">' in body, f"{label} missing on {page}"


def test_the_list_page_still_has_no_external_asset_reference(client, store):
    _make(store, "how ssds work", folder="tech")

    assert not re.search(r"https?://", client.get("/").text, re.IGNORECASE)
