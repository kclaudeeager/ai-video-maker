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
from videomaker.models import Status
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


@pytest.fixture
def waiting_now(monkeypatch):
    """Count a freshly-created project as waiting.

    A new project's derived status is `new` — the machine about to write a script,
    not a person at a gate — so nothing in a unit-test workspace is waiting under
    the real `WAITING_STATUSES`. Driving a project to a genuine gate needs a script,
    a voice and a storyboard; that is an integration concern, and the arithmetic of
    the rollup is covered as a pure unit in `test_folders.py`. What is left to test
    here is the wiring, so the *set* is what moves.
    """
    import videomaker.web.routes.projects as projects_mod

    monkeypatch.setattr(projects_mod, "WAITING_STATUSES", frozenset({Status.NEW}))


def _card_for(body: str, label: str) -> str:
    """The `<li class="folder-card">` for one label, from its marker to its close.

    A card is a flat, self-closing block — no nesting to bound, unlike the tree this
    replaced — so the next `</li>` really is the end of it.
    """
    after = body.split(f'data-folder="{label}"', 1)[1]
    return after[: after.index("</li>")]


def _projects_in(body: str) -> list[str]:
    return re.findall(r'data-project="([^"]*)"', body)


# ------------------------------------------------------- the root: folders only


def test_a_project_with_no_folder_stays_at_the_root_of_the_list(client, store):
    _make(store, "how ssds work")

    body = client.get("/").text

    assert 'data-project="how-ssds-work"' in body
    assert _folders_in(body) == [], "a flat workspace must draw no folder chrome at all"


def test_the_root_shows_a_folder_instead_of_the_projects_inside_it(client, store):
    """The whole point of the drill-down: ten projects behind two cards, not ten
    cards you have to read end to end. A project in a folder is reached *through*
    it, so it must not also be printed at the root."""
    _make(store, "how ssds work", folder="tech")
    _make(store, "my holiday", folder="personal")
    _make(store, "loose one")

    body = client.get("/").text

    assert sorted(_folders_in(body)) == ["personal", "tech"]
    assert _projects_in(body) == ["loose-one"], "only the unfiled project belongs here"


def test_the_root_shows_only_the_top_level_of_a_deep_label(client, store):
    """`tech/office-basics/word` puts one card at the root, not three."""
    _make(store, "deep one", folder="tech/office-basics/word")

    body = client.get("/").text

    assert _folders_in(body) == ["tech"]
    assert _projects_in(body) == []


def test_a_folder_card_is_a_plain_link_so_it_works_with_javascript_off(client, store):
    _make(store, "how ssds work", folder="tech")

    card = _card_for(client.get("/").text, "tech")

    assert '<a class="folder-link" href="/folders/tech"' in card
    assert "hx-" not in card, "navigating into a folder must not need htmx"


def test_the_folder_count_says_how_many_projects_are_beneath_it(client, store):
    """A `data-*` pair: the machine-readable count and the words must agree, and the
    count reaches through the children rather than stopping at this level."""
    _make(store, "one", folder="tech")
    _make(store, "two", folder="tech/office-basics")
    _make(store, "three", folder="tech/office-basics")
    _make(store, "elsewhere", folder="personal")

    root = client.get("/").text

    assert 'data-folder-count="3">3 projects<' in _card_for(root, "tech")
    assert 'data-folder-count="1">1 project<' in _card_for(root, "personal")
    assert 'data-folder-count="2">2 projects<' in _card_for(
        client.get("/folders/tech").text, "tech/office-basics"
    )


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


# ------------------------------------------------------------- drilling down


def test_a_folder_page_shows_the_projects_filed_directly_in_it(client, store):
    _make(store, "how ssds work", folder="tech")
    _make(store, "elsewhere", folder="personal")

    body = client.get("/folders/tech").text

    assert _projects_in(body) == ["how-ssds-work"]
    assert "elsewhere" not in body


def test_a_folder_page_shows_its_children_and_its_own_projects(client, store):
    _make(store, "parent project", folder="tech")
    _make(store, "child project", folder="tech/office-basics")

    body = client.get("/folders/tech").text

    assert _folders_in(body) == ["tech/office-basics"]
    assert _projects_in(body) == ["parent-project"], "the child is behind its own card"


def test_a_nested_folder_is_reached_by_its_full_label(client, store):
    _make(store, "deep one", folder="tech/office-basics/word")

    body = client.get("/folders/tech/office-basics/word").text

    assert _projects_in(body) == ["deep-one"]


def test_the_breadcrumb_links_every_level_above_the_one_you_are_on(client, store):
    _make(store, "deep one", folder="tech/office-basics/word")

    body = client.get("/folders/tech/office-basics/word").text
    crumbs = body.split('<nav class="crumbs"', 1)[1].split("</nav>", 1)[0]

    assert 'href="/"' in crumbs, "the root is always one click away"
    assert 'href="/folders/tech"' in crumbs
    assert 'href="/folders/tech/office-basics"' in crumbs
    # You are already here, so it is text rather than a link to the current page.
    assert 'href="/folders/tech/office-basics/word"' not in crumbs
    assert "word</span>" in crumbs


def test_a_folder_nothing_claims_is_a_404(client, store):
    """A folder exists only because a project claims its label, so there is no such
    thing as an empty one — and a typo in the address bar must not render as a real,
    permanently empty folder."""
    _make(store, "how ssds work", folder="tech")

    assert client.get("/folders/nope").status_code == 404
    assert client.get("/folders/tech/nope").status_code == 404


def test_the_bare_folders_path_is_a_404_rather_than_a_second_root(client, store):
    """The root is `/`. Serving it from two addresses would split the one page a
    person is meant to come back to."""
    _make(store, "how ssds work", folder="tech")

    assert client.get("/folders/").status_code == 404


# ----------------------------------------------------- what needs a human


def test_a_folder_card_says_how_many_projects_are_waiting_for_you(client, store, waiting_now):
    """Amber means one thing in this UI: someone is waiting on you."""
    _make(store, "one", folder="tech")
    _make(store, "two", folder="tech/office-basics")

    card = _card_for(client.get("/").text, "tech")

    # Reaches through the child, or the root card could never tell you there was a
    # reason to drill down at all.
    assert 'data-waiting="2"' in card


def test_the_root_summarises_what_needs_you_above_the_folders(client, store, waiting_now):
    _make(store, "one", folder="tech")
    _make(store, "two")

    body = client.get("/").text

    assert 'class="summary-strip" data-waiting="2"' in body
    assert body.index("summary-strip") < body.index("folder-card")


def test_nothing_waiting_prints_no_summary_strip_at_all(client, store, monkeypatch):
    """A dashboard that says `0 waiting for you` every day teaches you to stop
    reading it, so the strip is absent rather than zero."""
    import videomaker.web.routes.projects as projects_mod

    _make(store, "one", folder="tech")
    monkeypatch.setattr(projects_mod, "WAITING_STATUSES", frozenset())

    body = client.get("/").text

    assert "summary-strip" not in body
    assert "data-waiting" not in body, "and no amber badge on the folder either"


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

    # On the folder page, which is where a filed project's row now lives.
    body = client.get("/folders/tech").text

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
