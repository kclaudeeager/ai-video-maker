"""The M1 commands: `new`, `run`, `status`, `list`.

Every invocation passes `--providers mock`, so the whole pipeline runs offline.
The exit codes are part of the contract — `0` success, `1` stage failure, `2`
blocked at a gate — because a caller (M2's worker, CI, a shell script) has
nothing else to read.
"""

import json

import pytest
from typer.testing import CliRunner

from videomaker import runner as runner_module
from videomaker.cli import app
from videomaker.models import Status

runner = CliRunner()

TOPIC = "how ssds work"
PROJECT_ID = "how-ssds-work"
MINUTES = "0.5"


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    """A cwd-relative workspace and a throwaway user cache, so tests touch nothing real."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner_module, "USER_CACHE_DIR", tmp_path / "cache")
    return tmp_path / "workspace"


def _new(*args):
    return runner.invoke(app, ["new", TOPIC, "-t", "tech_explainer", "-m", MINUTES, *args])


def _run(*args):
    return runner.invoke(app, ["run", PROJECT_ID, "--providers", "mock", *args])


# --------------------------------------------------------------------------- new


def test_new_creates_a_project_and_prints_its_id(workspace):
    result = _new()

    assert result.exit_code == 0
    assert PROJECT_ID in result.output
    saved = json.loads((workspace / "projects" / PROJECT_ID / "project.json").read_text())
    assert saved["topic"] == TOPIC
    assert saved["template"] == "tech_explainer"
    assert saved["target_minutes"] == float(MINUTES)


def test_new_honours_the_voice_option(workspace):
    _new("--voice", "am_adam")

    saved = json.loads((workspace / "projects" / PROJECT_ID / "project.json").read_text())
    assert saved["voice"] == "am_adam"


def test_new_rejects_an_unknown_template():
    result = runner.invoke(app, ["new", TOPIC, "-t", "no_such_template"])

    assert result.exit_code == 1
    assert "no_such_template" in result.output


# --------------------------------------------------------------------------- run


def test_run_without_yes_stops_at_the_script_gate():
    _new()

    result = _run("--until", "voice")

    assert result.exit_code == 2
    assert "script" in result.output.lower()
    assert "review" in result.output.lower()


def test_run_with_yes_passes_the_gate_and_reports_the_new_status():
    _new()

    result = _run("--yes", "--until", "visuals")

    assert result.exit_code == 0
    assert Status.STORYBOARD_READY.value in result.output


def test_run_on_a_missing_project_exits_one():
    result = runner.invoke(app, ["run", "nope", "--providers", "mock"])

    assert result.exit_code == 1
    assert "nope" in result.output


def test_run_with_an_unknown_stage_exits_one():
    _new()

    result = _run("--yes", "--until", "nonsense")

    assert result.exit_code == 1
    assert "nonsense" in result.output


def test_a_stage_failure_exits_one(monkeypatch):
    _new()

    def boom(_project, _deps):
        raise RuntimeError("kokoro fell over")

    monkeypatch.setitem(runner_module.STAGE_RUNNERS, "voice", boom)

    result = _run("--yes", "--until", "voice")

    assert result.exit_code == 1
    assert "kokoro fell over" in result.output


def test_providers_mock_needs_no_credentials():
    """The default chain would reach for Groq; `--providers mock` must replace it all."""
    _new()

    result = _run("--yes", "--until", "script")

    assert result.exit_code == 0, result.output


# ------------------------------------------------------------------ status / list


def test_status_reports_the_derived_status_and_the_stages():
    _new()
    _run("--yes", "--until", "align")

    result = runner.invoke(app, ["status", PROJECT_ID])

    assert result.exit_code == 0
    assert Status.VOICED.value in result.output
    assert "align" in result.output
    assert "render" in result.output


def test_status_on_a_missing_project_exits_one():
    result = runner.invoke(app, ["status", "nope"])

    assert result.exit_code == 1


def test_list_shows_every_project_with_its_status():
    _new()

    result = runner.invoke(app, ["list"])

    assert result.exit_code == 0
    assert PROJECT_ID in result.output
    assert Status.NEW.value in result.output


def test_list_is_empty_but_successful_with_no_projects():
    result = runner.invoke(app, ["list"])

    assert result.exit_code == 0
