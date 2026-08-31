"""`videomaker clean` — the one command in this codebase that deletes files.

Every test here is a safety property, so they are written to fail loudly rather
than to describe behaviour: what survives matters more than what goes. Three of
them exist specifically to kill a mutant that a careless refactor could
introduce, and each says so in its docstring.

Nothing in this module touches a real workspace: the autouse fixture chdirs into
`tmp_path`, and `Settings.workspace_dir` defaults to `workspace` *relative to the
working directory*, so every path below is throwaway.
"""

import json

import pytest
from typer.testing import CliRunner

from videomaker import cleanup
from videomaker.cli import app
from videomaker.project import ProjectStore

runner = CliRunner()

TOPIC = "demo"
PROJECT_ID = "demo"

#: Rebuildable intermediates. Sizes are distinct so a total can only add up one way.
BUILD_FILES = {
    "build/video_wide.mp4": 4096,
    "build/narration_wide.wav": 2048,
    "build/s01_wide.mp4": 8192,
    "build/preview_wide.mp4": 1024,
}
#: The deliverables. Removed only when asked, never when `--keep-outputs` is passed.
OUTPUT_FILES = {"output/final_wide.mp4": 16384, "output/thumbnail.jpg": 512}
#: Downloaded footage: rebuildable, but only by spending provider quota again.
ASSET_FILES = {"scenes/s01/asset.mp4": 32768}
#: Hours of compute that `clean` must never be able to reach, in any mode.
PRECIOUS_FILES = {
    "scenes/s01/narration.wav": 256,
    "scenes/s01/words.json": 128,
    "captions/wide.ass": 64,
}

BUILD_BYTES = sum(BUILD_FILES.values())
OUTPUT_BYTES = sum(OUTPUT_FILES.values())
ASSET_BYTES = sum(ASSET_FILES.values())

#: A stage ledger with one key of every shape the pipeline writes.
STAGE_KEYS = {
    "script:all": "h",
    "voice:s01": "h",
    "align:s01": "h",
    "visuals:s01": "h",
    "captions:wide": "h",
    "assemble:wide": "h",
    "assemble:wide:s01": "h",
    "render:wide": "h",
    "preview:wide": "h",
    "preview-mix:wide": "h",
    "thumbnail:all": "h",
    "status:script:all": "h",
    "status:voice:s01": "h",
    "status:align:s01": "h",
    "status:visuals:s01": "h",
    "status:captions:wide": "h",
    "status:assemble:wide": "h",
    "status:render:wide": "h",
    "status:thumbnail:all": "h",
}

#: The stage keys that must survive every mode of `clean`: re-running must never
#: re-write the script, re-voice a scene or re-align a take.
UNTOUCHED_KEYS = (
    "script:all",
    "voice:s01",
    "align:s01",
    "status:script:all",
    "status:voice:s01",
    "status:align:s01",
)


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    """A cwd-relative workspace, so nothing outside `tmp_path` is reachable."""
    monkeypatch.chdir(tmp_path)
    return tmp_path / "workspace"


def _write(root, relpaths):
    for relpath, size in relpaths.items():
        path = root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)


def make_project(workspace, topic=TOPIC):
    """A project folder with one file of every kind the layout can hold."""
    store = ProjectStore(workspace)
    project = store.create(topic, "tech_explainer")
    root = store.path_for(project.id)
    _write(root, BUILD_FILES | OUTPUT_FILES | ASSET_FILES | PRECIOUS_FILES)
    (root / "cache" / "stages.json").write_text(json.dumps(STAGE_KEYS))
    return root


def stages(root):
    return json.loads((root / "cache" / "stages.json").read_text())


def clean(*args, **kwargs):
    return runner.invoke(app, ["clean", *args], **kwargs)


def exists(root, relpaths):
    return {relpath: (root / relpath).is_file() for relpath in relpaths}


def all_present(root, relpaths):
    return exists(root, relpaths) == dict.fromkeys(relpaths, True)


def none_present(root, relpaths):
    return exists(root, relpaths) == dict.fromkeys(relpaths, False)


# ------------------------------------------------------------------ what goes


def test_default_removes_the_build_intermediates(workspace):
    root = make_project(workspace)

    result = clean(PROJECT_ID, "--yes")

    assert result.exit_code == 0
    assert none_present(root, BUILD_FILES)


def test_default_leaves_the_outputs_alone(workspace):
    """`output/` is never touched unless explicitly asked for."""
    root = make_project(workspace)

    clean(PROJECT_ID, "--yes")

    assert all_present(root, OUTPUT_FILES)


def test_default_leaves_the_downloaded_assets_alone(workspace):
    root = make_project(workspace)

    clean(PROJECT_ID, "--yes")

    assert all_present(root, ASSET_FILES)


def test_all_removes_the_assets_and_the_outputs_too(workspace):
    root = make_project(workspace)

    result = clean(PROJECT_ID, "--all", "--yes")

    assert result.exit_code == 0
    assert none_present(root, BUILD_FILES)
    assert none_present(root, ASSET_FILES)
    assert none_present(root, OUTPUT_FILES)


def test_keep_outputs_genuinely_keeps_them(workspace):
    """MUTANT: ignoring `--keep-outputs` deletes the two finished videos here."""
    root = make_project(workspace)

    result = clean(PROJECT_ID, "--all", "--keep-outputs", "--yes")

    assert result.exit_code == 0
    assert all_present(root, OUTPUT_FILES)
    assert none_present(root, BUILD_FILES)
    assert none_present(root, ASSET_FILES)


def test_keep_outputs_leaves_the_outputs_out_of_the_plan(workspace):
    """Not merely spared at delete time: never listed, never counted, never warned about."""
    make_project(workspace)

    result = clean(PROJECT_ID, "--all", "--keep-outputs", "--dry-run")

    assert "output/" not in result.output


# ------------------------------------------------------- what can never go


def test_project_json_survives_every_mode(workspace):
    """Losing `project.json` loses the project whatever else is on disk."""
    root = make_project(workspace)

    clean(PROJECT_ID, "--all", "--yes")

    assert (root / "project.json").is_file()
    assert json.loads((root / "project.json").read_text())["id"] == PROJECT_ID


def test_the_takes_and_their_alignment_survive_every_mode(workspace):
    root = make_project(workspace)

    clean(PROJECT_ID, "--all", "--yes")

    assert all_present(root, PRECIOUS_FILES)


def test_apply_refuses_a_path_outside_the_removable_areas(workspace):
    """MUTANT: a guard that waves everything through deletes `project.json` here.

    This is the invariant, tested at the only place that can enforce it: a plan
    is handed to `apply_clean` naming a file no collector would ever have found,
    and it has to refuse rather than unlink it.
    """
    root = make_project(workspace)
    store = ProjectStore(workspace)
    forged = cleanup.CleanPlan(
        groups=(
            cleanup.Group(
                project_id=PROJECT_ID,
                root=root,
                category=cleanup.BUILD,
                label="build/",
                paths=(root / "project.json",),
                total_bytes=1,
            ),
        )
    )

    with pytest.raises(ValueError, match="refusing"):
        cleanup.apply_clean(store, forged)

    assert (root / "project.json").is_file()


def test_a_symlink_in_build_is_left_where_it_is(workspace):
    """A link is not an intermediate: it is not collected and it is not followed.

    The file it points at could never have gone — `unlink` removes links, not
    targets — but the *link* could have, and `clean` has no way to know what it
    was for. It stays, and so does everything on the other side of it.
    """
    root = make_project(workspace)
    outside = workspace.parent / "elsewhere.mp4"
    outside.write_bytes(b"precious")
    link = root / "build" / "link.mp4"
    link.symlink_to(outside)

    result = clean(PROJECT_ID, "--yes")

    assert result.exit_code == 0
    assert link.is_symlink()
    assert outside.is_file()
    assert none_present(root, BUILD_FILES)


# ------------------------------------------------------------- containment


def test_a_project_id_that_escapes_the_workspace_is_refused(workspace):
    make_project(workspace)

    result = clean("../../etc", "--yes")

    assert result.exit_code == 1
    assert "no such project" in result.output


def test_a_project_id_with_a_separator_is_refused(workspace):
    make_project(workspace)

    result = clean("demo/../../etc", "--yes")

    assert result.exit_code == 1


def test_a_symlinked_project_resolving_to_a_prefix_sibling_is_refused(workspace):
    """MUTANT: a `startswith` containment check empties `projects-evil/` here.

    `<workspace>/projects-evil` starts with `<workspace>/projects` as a *string*
    while being a different directory. `Path.is_relative_to` compares components,
    so it says no; a string prefix says yes and deletes someone else's files.
    """
    make_project(workspace)
    sibling = workspace / "projects-evil" / "victim"
    _write(sibling, BUILD_FILES)
    (workspace / "projects" / "victim").symlink_to(sibling)

    result = clean("victim", "--yes")

    assert result.exit_code == 1
    assert all_present(sibling, BUILD_FILES)


def test_a_missing_project_exits_one(workspace):
    make_project(workspace)

    result = clean("not-a-project", "--yes")

    assert result.exit_code == 1
    assert "no such project" in result.output


# ------------------------------------------------- printing before deleting


def test_the_plan_and_its_total_are_printed_before_anything_goes(workspace):
    root = make_project(workspace)

    result = clean(PROJECT_ID, "--yes")

    assert "build/" in result.output
    assert cleanup.human_bytes(BUILD_BYTES) in result.output
    assert none_present(root, BUILD_FILES)


def test_dry_run_lists_every_file_and_deletes_nothing(workspace):
    root = make_project(workspace)

    result = clean(PROJECT_ID, "--all", "--dry-run")

    assert result.exit_code == 0
    for relpath in BUILD_FILES | ASSET_FILES | OUTPUT_FILES:
        assert relpath in result.output
    assert cleanup.human_bytes(BUILD_BYTES + ASSET_BYTES + OUTPUT_BYTES) in result.output
    assert all_present(root, BUILD_FILES | ASSET_FILES | OUTPUT_FILES)


def test_dry_run_ignores_yes(workspace):
    """`--dry-run --yes` is a contradiction; the safe half wins."""
    root = make_project(workspace)

    clean(PROJECT_ID, "--dry-run", "--yes")

    assert all_present(root, BUILD_FILES)


# ------------------------------------------------------------ confirmation


def test_declining_the_prompt_deletes_nothing(workspace):
    root = make_project(workspace)

    result = clean(PROJECT_ID, input="n\n")

    assert result.exit_code == 1
    assert all_present(root, BUILD_FILES)


def test_no_answer_at_all_deletes_nothing(workspace):
    """A closed stdin — cron, a pipe, CI — must abort, never assume yes."""
    root = make_project(workspace)

    result = clean(PROJECT_ID, input="")

    assert result.exit_code != 0
    assert all_present(root, BUILD_FILES)


def test_accepting_the_prompt_deletes(workspace):
    root = make_project(workspace)

    result = clean(PROJECT_ID, input="y\n")

    assert result.exit_code == 0
    assert none_present(root, BUILD_FILES)


# ------------------------------------------------------------- the ledger


def test_the_cleaned_stages_go_stale_and_the_earlier_ones_do_not(workspace):
    """Status is derived from this ledger, so pending-ness is written here."""
    root = make_project(workspace)

    clean(PROJECT_ID, "--yes")

    left = stages(root)
    for key in ("assemble:wide", "assemble:wide:s01", "preview:wide", "preview-mix:wide"):
        assert key not in left
    for key in ("status:assemble:wide",):
        assert key not in left
    for key in UNTOUCHED_KEYS:
        assert key in left
    # `output/` was untouched, so the render really is still current.
    assert "render:wide" in left
    assert "status:render:wide" in left


def test_cleaning_the_outputs_makes_render_and_thumbnail_stale(workspace):
    root = make_project(workspace)

    clean(PROJECT_ID, "--all", "--yes")

    left = stages(root)
    for key in ("render:wide", "status:render:wide", "thumbnail:all", "status:thumbnail:all"):
        assert key not in left
    for key in UNTOUCHED_KEYS:
        assert key in left


def test_a_dry_run_leaves_the_ledger_alone(workspace):
    root = make_project(workspace)

    clean(PROJECT_ID, "--all", "--dry-run")

    assert stages(root) == STAGE_KEYS


# ------------------------------------------------------------ choosing targets


def test_a_project_or_everything_is_required(workspace):
    make_project(workspace)

    result = clean("--yes")

    assert result.exit_code == 1
    assert all_present(workspace / "projects" / PROJECT_ID, BUILD_FILES)


def test_a_project_and_everything_together_are_refused(workspace):
    make_project(workspace)

    result = clean(PROJECT_ID, "--everything", "--yes")

    assert result.exit_code == 1
    assert all_present(workspace / "projects" / PROJECT_ID, BUILD_FILES)


def test_everything_cleans_every_project(workspace):
    first = make_project(workspace, "one")
    second = make_project(workspace, "two")

    result = clean("--everything", "--yes")

    assert result.exit_code == 0
    assert none_present(first, BUILD_FILES)
    assert none_present(second, BUILD_FILES)
    assert all_present(first, OUTPUT_FILES)
    assert all_present(second, OUTPUT_FILES)


def test_an_already_clean_project_is_not_an_error(workspace):
    root = make_project(workspace)
    clean(PROJECT_ID, "--yes")

    result = clean(PROJECT_ID, "--yes")

    assert result.exit_code == 0
    assert "nothing to remove" in result.output
    assert all_present(root, PRECIOUS_FILES)


def test_an_empty_workspace_is_not_an_error(workspace):
    result = clean("--everything", "--yes")

    assert result.exit_code == 0
    assert "nothing to remove" in result.output


# ------------------------------------------------------------------- sizes


@pytest.mark.parametrize(
    ("size", "text"),
    [
        (0, "0 B"),
        (999, "999 B"),
        (1024, "1.0 KB"),
        (15360, "15.0 KB"),
        (1024 * 1024 * 3 // 2, "1.5 MB"),
        (3 * 1024**3, "3.0 GB"),
    ],
)
def test_human_bytes(size, text):
    assert cleanup.human_bytes(size) == text
