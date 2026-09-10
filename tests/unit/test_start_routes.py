"""`/start` and the three ways in — including the upload, which is the only route
in the project that takes a file from the browser.

The three ways are alternatives rather than a sequence, and
`test_the_ways_in_are_not_numbered` is `docs/ui-design.md` §12 in executable form:
numbering them would encode a sequence that is not there.
"""

import re
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from videomaker.config import Settings
from videomaker.corpus import importer as importer_module
from videomaker.corpus.catalogue import CATALOGUE
from videomaker.corpus.importer import import_work, list_works
from videomaker.runner import PROVIDER_KINDS
from videomaker.web.app import create_app
from videomaker.web.routes.start import MAX_UPLOAD_BYTES
from videomaker.web.worker import JobState

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "usfm"

#: Every mock except the corpus: these tests are about works on this disk.
CHAINS = {**{kind: ["mock"] for kind in PROVIDER_KINDS}, "corpus": ["bible"]}


@pytest.fixture(autouse=True)
def _own_notice(tmp_path, monkeypatch):
    monkeypatch.setattr(importer_module, "NOTICE_PATH", tmp_path / "NOTICE.md")


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(workspace_dir=tmp_path / "workspace", provider_chains=CHAINS)


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    return TestClient(app)


def upload(client, name: str, body: bytes, **data):
    return client.post(
        "/start/document", files={"document": (name, body)}, data=data, follow_redirects=False
    )


# ---------------------------------------------------------------- the three ways


def test_start_offers_exactly_three_ways_in(client):
    body = client.get("/start").text
    assert body.count('class="way"') == 3
    for href in ("/start/video", "/start/read", "/start/document"):
        assert f'href="{href}"' in body


def test_the_root_offers_the_same_three(client):
    assert client.get("/").text.count('class="way"') == 3


def test_the_ways_in_are_not_numbered(client):
    """They are alternatives, not a sequence — `docs/ui-design.md` §12. A numbered
    marker here would encode an order that does not exist."""
    ways = client.get("/start").text.split('<ul class="ways">', 1)[1].split("</ul>", 1)[0]

    assert not re.search(r">\s*0?[123]\s*[.·—]?\s*<", ways), "no numbered markers"
    assert "→" not in ways, "an arrow after a verb adds a glyph and no information"


def test_the_root_carries_no_all_caps_eyebrow_over_its_hero(client):
    """The eyebrow says what kind of thing follows; the root has one kind, so an
    eyebrow there would be decoration wearing a label's clothes (§12)."""
    head = client.get("/").text.split('<h1 class="hero-line">', 1)[0]
    eyebrow = re.search(r'<p class="eyebrow flush">(.*?)</p>', head, re.DOTALL)

    assert eyebrow is not None, "the shell still renders the slot"
    assert eyebrow.group(1).strip() == "", "and the root leaves it empty"


def test_the_hero_is_the_serif_line(client):
    assert '<h1 class="hero-line">Everything here goes the long way round.</h1>' in client.get("/").text


# ------------------------------------------------------------------ from an idea


def test_the_video_form_moved_here_intact(client):
    body = client.get("/start/video").text
    assert '<form class="card form-card" method="post" action="/projects">' in body
    assert '<select id="voice" name="voice">' in body


def test_a_rejected_create_comes_back_to_the_page_that_has_the_form(client):
    response = client.post("/projects", data={"topic": "", "template": "tech_explainer"})
    assert response.status_code == 422
    assert "Give the video a topic." in response.text
    assert 'action="/projects"' in response.text, "the form has to be on the page it returns"


# -------------------------------------------------------------------- from a work


def test_the_catalogue_shows_every_row_with_its_licence(client):
    from html import unescape

    # Compared after unescaping rather than before: Jinja escapes an apostrophe
    # as `&#39;` and `html.escape` writes `&#x27;` — the same character, so the
    # comparison has to be on the text and not on one of its two spellings.
    body = unescape(client.get("/start/read").text)
    for spec in CATALOGUE.values():
        assert spec.title in body
        assert spec.licence in body


def test_a_work_already_here_is_offered_to_open_rather_than_to_import(client, settings):
    import_work(
        CATALOGUE["fixture"].model_copy(update={"archive": str(FIXTURES)}), settings.workspace_dir
    )
    body = client.get("/start/read").text
    assert 'href="/library/fixture"' in body


def test_importing_an_unknown_work_is_refused_with_the_reason(client):
    response = client.post("/start/read", data={"work_id": "nope"}, follow_redirects=False)
    assert response.status_code == 422
    assert "nope" in response.text


def test_the_bundled_fixture_says_it_has_no_download(client):
    response = client.post("/start/read", data={"work_id": "fixture"}, follow_redirects=False)
    assert response.status_code == 422
    assert "library import fixture" in response.text


def test_importing_a_real_row_goes_on_the_queue_and_lands_back_on_the_shelf(
    client, app, monkeypatch
):
    """Not on to `/library/<id>`: the import is a download and a parse of 66 books
    on the queue, so for the next half-minute there is no work there to land on,
    and redirecting to it answered a button press with a 404."""
    submitted = {}

    def fake_submit(key, kind, fn):
        submitted["key"], submitted["kind"] = key, kind

    monkeypatch.setattr(app.state.jobs, "submit", fake_submit)
    response = client.post("/start/read", data={"work_id": "web"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/start/read"
    assert submitted == {"key": "import:web", "kind": "import"}


def test_the_shelf_reports_an_import_in_flight_and_stops_asking_when_it_ends(client, app):
    from videomaker.web.routes.start import import_job_key

    app.state.jobs._states[import_job_key("web")] = JobState(
        project_id=import_job_key("web"), kind="import", state="running",
        message="fetching World English Bible",
    )
    running = client.get("/start/read").text
    assert "fetching World English Bible" in running
    assert 'hx-trigger="every 2s"' in running, "it asks again while something runs"

    app.state.jobs._states[import_job_key("web")].state = "done"
    finished = client.get("/start/read").text
    assert "hx-trigger" not in finished, "and stops asking the moment nothing does"


def test_a_failed_import_says_so_and_offers_another_go(client, app):
    from videomaker.web.routes.start import import_job_key

    app.state.jobs._states[import_job_key("web")] = JobState(
        project_id=import_job_key("web"), kind="import", state="failed",
        error="the archive could not be read",
    )
    body = client.get("/start/read").text

    assert "could not bring it in" in body
    assert "the archive could not be read" in body
    assert "Try again" in body


# --------------------------------------------------------------- from your own file


def test_a_markdown_upload_becomes_a_work(client, settings):
    response = upload(client, "field-notes.md", b"# One\n\nA paragraph.\n\n# Two\n\nAnother.\n")

    assert response.status_code == 303
    assert response.headers["location"] == "/library/field-notes"
    (work, chapters) = list_works(settings.workspace_dir)[0]
    assert work.title == "field-notes"
    assert chapters == 2


def test_a_title_can_be_given(client, settings):
    upload(client, "notes.md", b"# One\n\nA paragraph.\n", title="  Field Notes  ")
    (work, _chapters) = list_works(settings.workspace_dir)[0]
    assert work.title == "Field Notes"


def test_an_epub_upload_becomes_a_work(client, settings, tmp_path):
    path = tmp_path / "book.epub"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("one.xhtml", "<html><body><h1>Ch</h1><p>Words here.</p></body></html>")
    response = upload(client, "book.epub", path.read_bytes())

    assert response.status_code == 303
    assert list_works(settings.workspace_dir)[0][0].id == "book"


def test_a_second_upload_of_the_same_name_gets_its_own_id(client, settings):
    upload(client, "notes.md", b"# One\n\nA paragraph.\n")
    upload(client, "notes.md", b"# One\n\nA different paragraph.\n")
    assert {work.id for work, _c in list_works(settings.workspace_dir)} == {"notes", "notes-2"}


@pytest.mark.parametrize("name", ["clip.mp4", "sheet.xlsx", "noextension", "report.pdf"])
def test_a_file_longhand_cannot_read_is_refused_before_it_is_written(
    client, settings, name, monkeypatch
):
    """Refused on the suffix, before a byte reaches the disk.

    `read_document` would refuse it too, a moment later and after staging it —
    which is why this asserts on the staging rather than on the message: the
    latter cannot tell the two guards apart.
    """
    import tempfile

    staged: list[str] = []
    monkeypatch.setattr(tempfile, "mkdtemp", lambda *a, **k: staged.append(a) or "/nonexistent")

    response = upload(client, name, b"whatever")

    assert response.status_code == 422
    assert staged == [], "a file we cannot read is never written down"
    assert ".md" in response.text, "and the refusal says what it can read"
    assert list_works(settings.workspace_dir) == []


def test_an_empty_file_is_refused(client, settings):
    response = upload(client, "nothing.md", b"")
    assert response.status_code == 422
    assert "empty" in response.text
    assert list_works(settings.workspace_dir) == []


def test_a_file_with_no_readable_text_is_refused_rather_than_500(client, settings):
    response = upload(client, "blank.md", b"\n\n   \n")
    assert response.status_code == 422
    assert "no readable text" in response.text
    assert list_works(settings.workspace_dir) == []


def test_a_file_over_the_ceiling_is_refused_before_it_is_all_read(client, settings):
    response = upload(client, "huge.txt", b"x" * (MAX_UPLOAD_BYTES + 1024))

    assert response.status_code == 422
    assert "larger than" in response.text
    assert list_works(settings.workspace_dir) == []


def test_the_upload_leaves_no_temporary_file_behind(client, settings, monkeypatch):
    import tempfile

    made: list[str] = []
    real = tempfile.mkdtemp

    def watched(*args, **kwargs):
        path = real(*args, **kwargs)
        made.append(path)
        return path

    monkeypatch.setattr(tempfile, "mkdtemp", watched)
    upload(client, "notes.md", b"# One\n\nA paragraph.\n")

    assert made, "the upload is staged on disk, not held in memory"
    assert not any(Path(path).exists() for path in made)


def test_a_traversing_filename_cannot_escape_the_staging_directory(client, settings):
    # Starlette hands the filename through unchanged, so the route takes its
    # basename before it becomes a path.
    response = upload(client, "../../../etc/passwd.md", b"# One\n\nA paragraph.\n")

    assert response.status_code == 303
    (work, _chapters) = list_works(settings.workspace_dir)[0]
    assert work.id == "passwd"
    assert not Path("/etc/passwd.md").exists()
