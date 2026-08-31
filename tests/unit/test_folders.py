"""Folders: a path-shaped **label** on a project, and deliberately nothing else.

Three properties are worth more than the feature itself, and each has a test here
that fails loudly if it is broken.

**A folder is a label, not a location.** `Project.folder` holds `tech/office-basics`
and the project still lives at `workspace/projects/<id>/`. Nesting the workspace
directories would turn `{project_id}` — which all 24 routes take, and which M2 Task 2
found is separately attacker-controlled in `web/media.py` — into a path, and it would
make every cache key ambiguous. `test_a_folder_never_creates_a_directory` pins that.

**The label can never be mistaken for a filesystem path.** `clean_folder` refuses a
leading `/`, a `..`, an empty level, a backslash and any control character, so even
code that is careless with it later cannot walk out of anywhere. The rejection list is
parametrised, and each entry is a real traversal or injection shape rather than a
variation on the same one.

**Filing a project re-renders nothing.** This is the property M3 Task 22 paid for the
hard way: adding `short_beats` to the template hash staled `script:all` for every
project on disk, and `run_script` replaces `project.scenes` wholesale — a re-run would
have destroyed every voiced take, chosen shot and approval. Moving a project between
folders changes nothing rendered, so it must move no unit hash at all.
`test_filing_a_project_moves_no_unit_hash` captures every fingerprint before and after
and asserts they are byte-identical.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from videomaker.models import Project, clean_folder

#: The same real pre-change project the `preview_url` and `short_beats`
#: compatibility tests use: a `project.json` written before `folder` existed,
#: carrying scenes, outputs and all three approvals.
_OLD_PROJECT = Path(__file__).parent.parent / "fixtures" / "project_pre_preview_url.json"


# ------------------------------------------------------------------ clean_folder


def test_the_empty_label_is_the_root():
    assert clean_folder("") == ""
    assert clean_folder("   ") == ""


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("tech", "tech"),
        ("tech/office-basics", "tech/office-basics"),
        ("a/b/c/d", "a/b/c/d"),
        ("  tech/office-basics  ", "tech/office-basics"),
        ("tech / office basics", "tech/office basics"),
        ("Office Basics", "Office Basics"),
    ],
)
def test_a_relative_label_is_kept_with_its_levels_trimmed(given, expected):
    assert clean_folder(given) == expected


@pytest.mark.parametrize(
    "label",
    [
        "/tech",                 # absolute
        "/",                     # absolute, and nothing else
        "..",                    # the traversal itself
        "../tech",               # traversal at the front
        "tech/../../etc",        # traversal in the middle
        "tech/..",               # traversal at the end
        ".",                     # "here", which is not a folder name
        "tech/./office",         # a no-op level
        "tech//office",          # an empty level
        "tech/",                 # a trailing separator, i.e. an empty last level
        "tech/ /office",         # a level that is only whitespace
        "tech\\office",          # a Windows separator
        "\\tech",                # a Windows absolute-ish label
        "tech\x00office",        # a NUL, the classic path-truncation trick
        "tech\noffice",          # a newline, which would break any line-based use
        "tech\x7foffice",        # DEL
    ],
)
def test_a_label_that_could_be_read_as_a_path_is_refused(label):
    with pytest.raises(ValueError):
        clean_folder(label)


def test_the_refusal_says_what_is_wrong():
    """The message reaches a person through the form, so it must not be `ValueError()`."""
    for label in ("/tech", "../tech", "tech//office", "tech\\office"):
        with pytest.raises(ValueError) as raised:
            clean_folder(label)
        assert str(raised.value).strip(), f"no message for {label!r}"


def test_an_absolute_label_is_refused_as_absolute_rather_than_as_an_empty_level():
    """`/tech` would already fail the empty-level rule, so the leading-slash check
    earns its place only by naming what is actually wrong. Someone who typed a path
    needs to be told this is not one — not that their first level was blank."""
    with pytest.raises(ValueError) as raised:
        clean_folder("/tech")

    assert "start with /" in str(raised.value)


# ------------------------------------------------------------- the model field


def _project(**kw) -> Project:
    fields = {
        "id": "how-ssds-work",
        "topic": "how ssds work",
        "template": "tech_explainer",
        "created_at": datetime.now(UTC),
    }
    fields.update(kw)
    return Project(**fields)


def test_a_new_project_is_at_the_root():
    assert _project().folder == ""


def test_the_model_normalises_the_label_it_is_given():
    assert _project(folder="  tech / office-basics ").folder == "tech/office-basics"


def test_the_model_refuses_a_traversal_label():
    with pytest.raises(ValueError):
        _project(folder="../../etc")


def test_the_label_survives_a_round_trip_through_json():
    filed = _project(folder="tech/office-basics")
    assert Project.model_validate_json(filed.model_dump_json()).folder == "tech/office-basics"


# ------------------------------------------------------- backward compatibility


def test_a_project_json_written_before_folders_loads_at_the_root():
    """Ten finished projects must open on this version exactly as they were."""
    blob = _OLD_PROJECT.read_text()
    assert '"folder"' not in blob, "the fixture must hold the PRE-change shape"

    project = Project.model_validate_json(blob)

    assert project.folder == ""
    assert project.scenes, "the fixture must actually carry scenes"
    assert project.approvals.preview is not None, "and its approvals"


# --------------------------------------------------- the fingerprint guarantee


def _fingerprints(project: Project) -> dict[str, str]:
    from videomaker.runner import STAGE_UNITS

    return {
        f"{stage}:{unit.unit}": unit.fingerprint
        for stage, units_for in STAGE_UNITS.items()
        for unit in units_for(project)
    }


def test_filing_a_project_moves_no_unit_hash():
    """The whole reason `folder` is safe to add, pinned the way M3 Task 22 pinned it.

    `derive_status` returns at the first unit whose fingerprint moved, so one changed
    hash here reads as "re-run everything from this stage" — and for `script:all`
    that means `run_script` replacing `project.scenes`, discarding the voiced takes,
    the chosen shots and the approvals a person spent an evening on. Filing a project
    is a librarian's act; it must cost nothing.
    """
    project = Project.model_validate_json(_OLD_PROJECT.read_text())
    before = _fingerprints(project)
    assert before, "the fixture must produce units to compare"

    project.folder = "tech/office-basics"
    after = _fingerprints(project)

    assert after == before
    # And again from a different label, so a fingerprint that happened to collide
    # for one value would still be caught.
    project.folder = "personal"
    assert _fingerprints(project) == before


def test_filing_a_project_changes_no_derived_status(tmp_path):
    """End to end: the status a person sees is the same before and after the move."""
    from videomaker.project import ProjectStore
    from videomaker.runner import derive_status, stage_cache_for

    store = ProjectStore(tmp_path / "workspace")
    project = store.create("how ssds work", "tech_explainer")
    before = derive_status(project, stage_cache_for(store, project.id))

    project.folder = "tech/office-basics"
    store.save(project)

    reloaded = store.load(project.id)
    assert reloaded.folder == "tech/office-basics"
    assert derive_status(reloaded, stage_cache_for(store, project.id)) == before


# ------------------------------------------------------------- the store's side


def test_a_folder_never_creates_a_directory(tmp_path):
    """The label is metadata. The workspace layout is exactly what it always was."""
    from videomaker.project import ProjectStore

    store = ProjectStore(tmp_path / "workspace")
    project = store.create("how ssds work", "tech_explainer", folder="tech/office-basics")

    assert project.folder == "tech/office-basics"
    assert store.path_for(project.id).is_dir()
    assert store.path_for(project.id) == store.projects_dir / project.id
    assert sorted(p.name for p in store.projects_dir.iterdir()) == [project.id]
    assert not (store.projects_dir / "tech").exists()


def test_the_folder_is_written_to_disk(tmp_path):
    import json

    from videomaker.project import ProjectStore

    store = ProjectStore(tmp_path / "workspace")
    project = store.create("how ssds work", "tech_explainer", folder="tech")

    saved = json.loads((store.path_for(project.id) / "project.json").read_text())
    assert saved["folder"] == "tech"


def test_the_store_lists_every_folder_label_in_use_with_its_ancestors(tmp_path):
    """A parent with no project of its own is still a folder: `tech` exists because
    `tech/office-basics` does. Without the ancestors the tree would have a hole in it
    and the datalist would not offer the level a person is most likely to want."""
    from videomaker.project import ProjectStore

    store = ProjectStore(tmp_path / "workspace")
    store.create("a", "tech_explainer", folder="tech/office-basics")
    store.create("b", "tech_explainer", folder="tech/office-basics")
    store.create("c", "tech_explainer", folder="personal")
    store.create("d", "tech_explainer")

    assert store.folders() == ["personal", "tech", "tech/office-basics"]


def test_the_folder_list_is_empty_on_a_flat_workspace(tmp_path):
    from videomaker.project import ProjectStore

    store = ProjectStore(tmp_path / "workspace")
    store.create("a", "tech_explainer")

    assert store.folders() == []
