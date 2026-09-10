"""The workspace projection: one list over two stores, and no `Project` for a work.

The test that matters is `test_a_work_never_needs_you`. Reading has no gates and
asks nothing of anybody; the moment a work could report itself as waiting, someone
has given the reader a state machine, which is the thing this design exists
without.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from videomaker.config import Settings
from videomaker.corpus import importer as importer_module
from videomaker.corpus.catalogue import CATALOGUE
from videomaker.corpus.importer import import_work
from videomaker.models import Status
from videomaker.project import ProjectStore
from videomaker.runner import PROVIDER_KINDS, build_deps, run_pipeline
from videomaker.web.workspace import (
    STATUS_BEFORE_GATE,
    Kind,
    WorkspaceItem,
    counts,
    waiting,
    workspace_items,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "usfm"


@pytest.fixture(autouse=True)
def _own_notice(tmp_path, monkeypatch):
    monkeypatch.setattr(importer_module, "NOTICE_PATH", tmp_path / "NOTICE.md")


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        workspace_dir=tmp_path / "workspace",
        provider_chains={kind: ["mock"] for kind in PROVIDER_KINDS},
    )


@pytest.fixture
def store(settings) -> ProjectStore:
    return ProjectStore(settings.workspace_dir)


def a_work(settings):
    return import_work(
        CATALOGUE["fixture"].model_copy(update={"archive": str(FIXTURES)}),
        settings.workspace_dir,
    )


def a_project(settings, store, topic="how ssds work", *, scripted=False, cache_dir=None):
    project = store.create(topic, "tech_explainer", target_minutes=0.5)
    if scripted:
        deps = build_deps(settings, project.id, cache_dir=cache_dir)
        run_pipeline(project, deps, until="script")
    return project


# ------------------------------------------------------------------- the list


def test_an_empty_workspace_is_empty(settings, store):
    assert workspace_items(settings, store) == []
    assert counts([]) == {"video": 0, "reading": 0}


def test_a_project_and_a_work_appear_together(settings, store):
    a_project(settings, store)
    a_work(settings)
    items = workspace_items(settings, store)
    assert {item.kind for item in items} == {Kind.VIDEO, Kind.READING}
    assert [item.title for item in items if item.kind is Kind.READING] == [
        "Longhand Test Fixture"
    ]
    assert counts(items) == {"video": 1, "reading": 1}


def test_each_row_links_to_its_own_page(settings, store):
    project = a_project(settings, store)
    work = a_work(settings)
    hrefs = {item.kind: item.href for item in workspace_items(settings, store)}
    assert hrefs[Kind.VIDEO] == f"/projects/{project.id}"
    assert hrefs[Kind.READING] == f"/library/{work.id}"


def test_the_newest_thing_is_first(settings, store):
    a_work(settings)
    project = a_project(settings, store)
    # `create` stamps `created_at` now; the work's mtime is a moment earlier.
    project.created_at = datetime.now(UTC) + timedelta(hours=1)
    store.save(project)
    assert workspace_items(settings, store)[0].kind is Kind.VIDEO


def test_a_project_directory_that_will_not_load_is_skipped(settings, store):
    a_project(settings, store)
    broken = store.projects_dir / "half-written"
    broken.mkdir(parents=True)
    (broken / "project.json").write_text("{not json")
    items = workspace_items(settings, store)
    assert [item.id for item in items] == ["how-ssds-work"]


# ------------------------------------------------------------------ needs you


def test_a_work_never_needs_you(settings, store):
    a_work(settings)
    (item,) = workspace_items(settings, store)
    assert item.kind is Kind.READING
    assert item.needs_you is False
    assert item.tone == ""
    assert waiting(workspace_items(settings, store)) == []
    assert "chapters" in item.detail


def test_a_project_waiting_at_a_gate_says_which_one(settings, store, tmp_path):
    a_project(settings, store, scripted=True, cache_dir=tmp_path / "cache")
    (item,) = workspace_items(settings, store)
    assert item.needs_you is True
    assert item.detail == "waiting at the script gate"
    assert item.tone == "warn"
    assert waiting(workspace_items(settings, store)) == [item]


def test_a_project_with_nothing_done_yet_is_not_waiting_on_you(settings, store):
    a_project(settings, store)
    (item,) = workspace_items(settings, store)
    assert item.needs_you is False
    assert item.detail == "new"


def test_an_approved_gate_stops_the_project_asking(settings, store, tmp_path):
    project = a_project(settings, store, scripted=True, cache_dir=tmp_path / "cache")
    project.approvals.script = datetime.now(UTC)
    store.save(project)
    (item,) = workspace_items(settings, store)
    assert item.needs_you is False, "an approved gate is not a gate you are standing at"
    assert item.detail == "script ready"


def test_the_gate_table_covers_every_gate():
    from videomaker.runner import GATE_BEFORE

    assert set(STATUS_BEFORE_GATE) == set(GATE_BEFORE.values())
    assert set(STATUS_BEFORE_GATE.values()) <= set(Status)


def test_the_item_is_a_plain_value(settings, store):
    a_work(settings)
    (item,) = workspace_items(settings, store)
    assert isinstance(item, WorkspaceItem)
    assert item.model_dump()["kind"] == "reading"
