"""A passage becomes an ordinary project — and the pipeline does not notice.

Two guarantees, and the second is the one that would cost a day if it broke.

`test_watching_twice_makes_one_project`: the unit key is the identity, so
pressing Watch again picks the project back up instead of leaving a second
half-finished one behind.

`test_a_source_moves_no_fingerprint`: `Project.source` feeds no stage hash. If it
ever did, the next run would derive every materialised project as stale, and
`run_script` replaces `project.scenes` **wholesale** — taking every voiced take,
chosen shot and approval with it (M3 Task 22).
"""

import json

import pytest

from videomaker.cache import StageCache
from videomaker.corpus.materialise import (
    DEFAULT_TEMPLATE,
    existing_for,
    materialise,
    source_for,
)
from videomaker.corpus.models import UnitRef, UnitText, Verse
from videomaker.models import Project, SourceRef
from videomaker.project import ProjectStore
from videomaker.runner import PROVIDER_KINDS, STAGE_UNITS, stamp_stage

REF = UnitRef(work_id="web", book="JHN", chapter=3)


@pytest.fixture
def store(tmp_path) -> ProjectStore:
    return ProjectStore(tmp_path / "workspace")


@pytest.fixture
def unit() -> UnitText:
    return UnitText(
        ref=REF, title="John 3", verses=[Verse(number=1, text="A verse of the passage.")]
    )


# ------------------------------------------------------------------- the project


def test_a_passage_becomes_a_project_filed_under_its_work(store, unit):
    project = materialise(REF, unit=unit, store=store)

    assert project.topic == "John 3"
    assert project.folder == "web/John"
    assert project.template == DEFAULT_TEMPLATE
    assert project.source == SourceRef(work_id="web", unit_key="web/JHN/003")
    assert project.thumbnail_text == "John 3"
    assert store.load(project.id).source == project.source


def test_watching_twice_makes_one_project(store, unit):
    first = materialise(REF, unit=unit, store=store)
    second = materialise(REF, unit=unit, store=store)

    assert second.id == first.id
    assert store.list_ids() == [first.id]


def test_a_different_passage_is_a_different_project(store, unit):
    materialise(REF, unit=unit, store=store)
    other = UnitRef(work_id="web", book="JHN", chapter=4)
    materialise(other, unit=unit.model_copy(update={"ref": other}), store=store)

    assert len(store.list_ids()) == 2


def test_the_same_chapter_of_another_work_is_its_own_project(store, unit):
    materialise(REF, unit=unit, store=store)
    other = UnitRef(work_id="bsb", book="JHN", chapter=3)
    materialise(other, unit=unit.model_copy(update={"ref": other}), store=store)

    assert len(store.list_ids()) == 2


def test_a_project_started_from_a_topic_has_no_source(store):
    project = store.create("how ssds work", "tech_explainer", target_minutes=0.5)

    assert project.source is None
    assert existing_for(store, REF) is None


def test_a_project_that_will_not_load_does_not_stop_the_search(store, unit):
    materialise(REF, unit=unit, store=store)
    broken = store.projects_dir / "half-written"
    broken.mkdir(parents=True)
    (broken / "project.json").write_text("{not json")

    assert existing_for(store, REF) is not None


def test_source_for_is_the_unit_key():
    assert source_for(REF) == SourceRef(work_id="web", unit_key="web/JHN/003")


# ------------------------------------------------- and the pipeline does not care


def test_a_project_written_before_this_field_loads_unchanged(store, tmp_path):
    """Every `project.json` on disk predates `source`; none may need rewriting."""
    project = store.create("how ssds work", "tech_explainer", target_minutes=0.5)
    path = store.path_for(project.id) / "project.json"
    on_disk = json.loads(path.read_text())
    del on_disk["source"]
    path.write_text(json.dumps(on_disk))

    assert store.load(project.id).source is None


def test_a_source_moves_no_fingerprint(store, unit, tmp_path):
    """Recording where a project came from changes nothing the pipeline computes."""
    plain = store.create("John 3", "tech_explainer", target_minutes=2.0)
    cache = StageCache(tmp_path / "stages.json")
    stamp_stage(plain, cache, "script")
    before = dict(cache._hashes)

    plain.source = SourceRef(work_id="web", unit_key="web/JHN/003")
    store.save(plain)
    after = StageCache(tmp_path / "stages.json")
    stamp_stage(store.load(plain.id), after, "script")

    assert dict(after._hashes) == before


@pytest.mark.parametrize("stage", sorted(STAGE_UNITS))
def test_no_stage_reads_the_source(store, stage):
    """Every stage's units, with and without a `source`, hash the same."""
    project = store.create("John 3", "tech_explainer", target_minutes=2.0)
    without = [unit.fingerprint for unit in STAGE_UNITS[stage](project)]

    project.source = SourceRef(work_id="web", unit_key="web/JHN/003")
    with_source = [unit.fingerprint for unit in STAGE_UNITS[stage](project)]

    assert with_source == without


def test_the_project_model_still_accepts_no_source():
    assert Project(id="x", topic="t", template="tech_explainer", created_at=__import__(
        "datetime"
    ).datetime.now(__import__("datetime").UTC)).source is None


def test_provider_kinds_are_untouched():
    assert "corpus" in PROVIDER_KINDS
