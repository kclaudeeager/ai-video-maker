"""Importing a work on a server that has no other way to get one.

A reading server mounts no route that can import, and a container on a free plan
has neither a persistent disk nor a shell. `IMPORT_WORKS` is the only way in, and
`test_a_reading_server_publishes_what_it_preloads` is the guarantee that makes it
worth anything: an unpublished work is invisible there, so an import that did not
publish would leave the shelf exactly as empty as before.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from videomaker.config import Audience, Settings
from videomaker.corpus.importer import ImportSpec, list_works, read_work, work_dir
from videomaker.web.app import create_app

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "usfm"

SPEC = ImportSpec(
    work_id="fixture",
    title="Longhand Test Fixture",
    language="en",
    licence="AGPL-3.0-only; invented text.",
    licence_url="https://www.gnu.org/licenses/agpl-3.0.html",
    source_url="https://example.invalid/fixture",
    archive=str(FIXTURE_DIR),
)


@pytest.fixture
def catalogue(monkeypatch):
    """One local row, so nothing in this file reaches the network."""
    monkeypatch.setattr("videomaker.corpus.catalogue.CATALOGUE", {"fixture": SPEC})
    return SPEC


def run(tmp_path, *, audience=Audience.READER, works="fixture"):
    """Boot the app the way a platform does, and wait for the preload to land."""
    app = create_app(
        Settings(workspace_dir=tmp_path / "workspace", audience=audience, import_works=works)
    )
    with TestClient(app) as client:
        app.state.jobs.wait_idle(120)
        yield_client = client.get("/healthz")
    assert yield_client.status_code == 200
    return app


def test_the_named_work_is_imported(tmp_path, catalogue):
    app = run(tmp_path)

    assert [work.id for work, _chapters in list_works(app.state.settings.workspace_dir)] == [
        "fixture"
    ]


def test_a_reading_server_publishes_what_it_preloads(tmp_path, catalogue):
    """An unpublished work is a 404 on a reading server, so importing without
    publishing would leave the shelf as empty as it started."""
    app = run(tmp_path, audience=Audience.READER)

    work = read_work(work_dir(app.state.settings.workspace_dir, "fixture"))
    assert work.published is True


def test_the_shelf_shows_it(tmp_path, catalogue):
    app = run(tmp_path)

    with TestClient(app) as client:
        body = client.get("/library").text

    assert "Longhand Test Fixture" in body


def test_a_studio_imports_but_does_not_publish(tmp_path, catalogue):
    """Importing a text and putting it in front of other people are two decisions.

    A studio operator makes the second one in the UI; a reading server has no UI
    for it, which is the only reason the preload makes it there.
    """
    app = run(tmp_path, audience=Audience.STUDIO)

    work = read_work(work_dir(app.state.settings.workspace_dir, "fixture"))
    assert work.published is False


def test_nothing_is_imported_when_the_setting_is_empty(tmp_path, catalogue):
    """A local run imports through the UI, where the person doing it sees the
    licence they are accepting."""
    app = run(tmp_path, works="")

    assert list_works(app.state.settings.workspace_dir) == []


def test_an_unknown_id_is_skipped_and_the_others_still_land(tmp_path, catalogue):
    """A typo in one id must not cost the rest, and must not stop the server."""
    app = run(tmp_path, works="nosuchwork,fixture")

    assert work_dir(app.state.settings.workspace_dir, "fixture").is_dir()


def test_an_unknown_id_alone_leaves_a_working_server(tmp_path, catalogue):
    app = run(tmp_path, works="nosuchwork")

    with TestClient(app) as client:
        assert client.get("/library").status_code == 200


def test_an_unknown_id_queues_no_job_at_all(tmp_path, catalogue):
    """Skipped where it is named, not left to fail inside the worker.

    Submitting it anyway "works" — the queue catches the exception — but it puts a
    failed import in `states()` that the catalogue page reports as a real attempt,
    and buries the actual cause (a typo) in a traceback nobody reads.
    """
    from videomaker.web.routes.start import import_job_key

    app = run(tmp_path, works="nosuchwork")

    assert app.state.jobs.state_for(import_job_key("nosuchwork")) is None
    assert app.state.jobs.states() == {}


def test_a_work_already_on_disk_is_not_fetched_again(tmp_path, catalogue):
    """Re-importing is a download and a parse of 66 books to arrive at the files
    that are already there. On a platform with a disk, that is every restart."""
    app = run(tmp_path)
    stamp = (work_dir(app.state.settings.workspace_dir, "fixture") / "work.yaml").stat().st_mtime_ns

    again = create_app(
        Settings(
            workspace_dir=app.state.settings.workspace_dir,
            audience=Audience.READER,
            import_works="fixture",
        )
    )
    with TestClient(again):
        again.state.jobs.wait_idle(120)

    after = (work_dir(app.state.settings.workspace_dir, "fixture") / "work.yaml").stat()
    assert after.st_mtime_ns == stamp


def test_the_health_check_answers_while_the_import_is_still_running(tmp_path, catalogue):
    """The preload runs on the job queue for exactly this reason: a platform health
    check starts when the process does, and a boot blocked on a Bible download is a
    container the platform kills before it ever answers."""
    app = create_app(
        Settings(
            workspace_dir=tmp_path / "workspace",
            audience=Audience.READER,
            import_works="fixture",
        )
    )
    with TestClient(app) as client:
        # Deliberately no `wait_idle`: this is the window the platform probes in.
        assert client.get("/healthz").status_code == 200
        app.state.jobs.wait_idle(120)


# ------------------------------------------------- what an empty shelf may say


def test_an_empty_reading_server_offers_no_route_it_does_not_have(tmp_path):
    """`/start/read` and `/start/document` are studio routes. Offering them on a
    reading server sent a visitor to a 404 — and the person who could fix an empty
    shelf is not the person reading it."""
    app = create_app(Settings(workspace_dir=tmp_path / "workspace", audience=Audience.READER))

    with TestClient(app) as client:
        body = client.get("/library").text
        assert client.get("/start/read").status_code == 404

    assert "/start/" not in body
    assert "Nothing published yet" in body


def test_an_empty_studio_still_offers_both_ways_in(tmp_path):
    app = create_app(Settings(workspace_dir=tmp_path / "workspace", audience=Audience.STUDIO))

    with TestClient(app) as client:
        body = client.get("/library").text

    assert 'href="/start/read"' in body
    assert 'href="/start/document"' in body


def test_no_test_can_append_to_the_projects_own_notice():
    """`NOTICE_PATH` is relative to the working directory, which during a test run
    is the repository.

    `import_work` appends the licence of everything it fetches, so a test that
    imported without redirecting it edited the project's own `NOTICE.md` — and a
    test that had monkeypatched `CATALOGUE` wrote its fixture's invented source
    URL in there as a bundled text. The suite cannot see that damage: every test
    still passes. `tests/conftest.py` redirects it for the whole session; this is
    the assertion that the redirect is still in place.
    """
    from videomaker.corpus import importer

    repo = Path(__file__).resolve().parents[2]

    assert importer.NOTICE_PATH.is_absolute(), "a relative path resolves against the repo"
    assert not importer.NOTICE_PATH.is_relative_to(repo)
