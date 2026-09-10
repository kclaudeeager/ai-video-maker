"""The root as a workspace: a video project and a work in one list.

`test_a_work_appears_next_to_a_project` is the whole unification, executed — and
`test_opening_the_root_creates_no_project_for_a_work` is the guard on it: the
moment a work materialises a `Project`, the reader is inside `STAGE_ORDER` and
everything the MVP plan protected is gone.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from videomaker.config import Settings
from videomaker.corpus import importer as importer_module
from videomaker.corpus.catalogue import CATALOGUE
from videomaker.corpus.documents import DocumentSpec, import_document
from videomaker.corpus.importer import import_work
from videomaker.project import ProjectStore
from videomaker.runner import PROVIDER_KINDS
from videomaker.web.app import create_app

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "usfm"
CHAINS = {**{kind: ["mock"] for kind in PROVIDER_KINDS}, "corpus": ["bible"]}


@pytest.fixture(autouse=True)
def _own_notice(tmp_path, monkeypatch):
    monkeypatch.setattr(importer_module, "NOTICE_PATH", tmp_path / "NOTICE.md")


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(workspace_dir=tmp_path / "workspace", provider_chains=CHAINS)


@pytest.fixture
def store(settings) -> ProjectStore:
    return ProjectStore(settings.workspace_dir)


@pytest.fixture
def client(settings):
    return TestClient(create_app(settings))


def a_work(settings):
    return import_work(
        CATALOGUE["fixture"].model_copy(update={"archive": str(FIXTURES)}), settings.workspace_dir
    )


# ------------------------------------------------------------------- both kinds


def test_a_work_appears_next_to_a_project(client, settings, store):
    store.create("how ssds work", "tech_explainer", target_minutes=0.5)
    a_work(settings)

    body = client.get("/").text

    assert 'data-project="how-ssds-work"' in body
    assert 'data-work="fixture"' in body
    assert "Longhand Test Fixture" in body
    assert 'href="/library/fixture"' in body


def test_a_work_reports_its_size_rather_than_a_status(client, settings):
    a_work(settings)
    body = client.get("/").text

    assert "6 chapters" in body
    assert "data-status" not in body, "a reading has no status to derive"


def test_a_document_of_your_own_is_listed_the_same_way(client, settings, tmp_path):
    source = tmp_path / "field-notes.md"
    source.write_text("# One\n\nA paragraph.\n")
    import_document(source, DocumentSpec(work_id="notes", title="Field Notes"), settings.workspace_dir)

    body = client.get("/").text

    assert 'data-work="notes"' in body
    assert "Field Notes" in body


def test_a_workspace_with_only_a_work_in_it_is_not_a_first_run(client, settings):
    a_work(settings)
    body = client.get("/").text

    assert "Nothing open yet" not in body
    # The first-run screen argues for making a video; someone who has imported a
    # book has started, and being told to start again would be wrong.
    assert "Start your first video" not in body


def test_an_empty_workspace_invites_rather_than_apologises(client):
    body = client.get("/").text

    assert "Nothing open yet" in body
    assert "no projects" in body.lower()
    assert body.count('class="way"') == 3, "the invitation is the three ways in"


# ------------------------------------------------------------- and no `Project`


def test_opening_the_root_creates_no_project_for_a_work(client, settings, store):
    """The guard on the whole design. A work is projected onto the same row type,
    not turned into one — `STAGE_ORDER` never learns it exists."""
    a_work(settings)

    client.get("/")

    assert store.list_ids() == []
    assert not (settings.workspace_dir / "projects").exists()


def test_the_reading_row_carries_no_pipeline_handles(client, settings):
    a_work(settings)
    row = client.get("/").text.split('data-work="fixture"', 1)[1].split("</li>", 1)[0]

    assert "data-project=" not in row
    assert "gate" not in row.lower()
