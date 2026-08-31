"""`clean` must leave a project that rebuilds — for free, and for real.

The unit tests prove which files go. This one proves the thing that actually
matters afterwards: a cleaned project is not a damaged project. It drives the
Typer app through a genuine `run` (mock providers, real FFmpeg), cleans it,
reads the status view, and runs it again with every provider method counted.

Two claims, and both are counted rather than described:

1. **`videomaker status` tells the truth.** After `clean`, `assemble` reads
   `pending` — its output is gone — while `script`, `voice` and `align` still
   read `current`, because their artefacts never lived in `build/`.
2. **The re-run spends nothing.** Not one call to the LLM, the TTS, the STT or
   the stock provider. If `clean` had taken `cache/stages.json`, or a take, or
   the words, this count would not be zero and the rebuild would cost the two
   most expensive stages in the pipeline all over again.
"""

import re
from collections import Counter

import pytest
from typer.testing import CliRunner

from videomaker import runner as runner_module
from videomaker.cli import app
from videomaker.models import Aspect
from videomaker.pipeline.assemble import narration_relpath, video_relpath
from videomaker.pipeline.render import output_relpath
from videomaker.project import ProjectStore
from videomaker.providers import mock as mock_module

runner = CliRunner()

TOPIC = "how ssds work"
PROJECT_ID = "how-ssds-work"
MINUTES = "0.5"

#: Every mock provider method, labelled by the quota a real one would have spent.
PROVIDER_METHODS: tuple[tuple[type, str, str], ...] = (
    (mock_module.MockLLM, "generate", "llm"),
    (mock_module.MockTTS, "synthesize", "tts"),
    (mock_module.MockSTT, "transcribe_words", "stt"),
    (mock_module.MockStock, "search", "stock.search"),
    (mock_module.MockStock, "download", "stock.download"),
    (mock_module.MockImage, "generate_image", "image"),
)

#: `build/` artefacts that must be gone after `clean` and back after the re-run.
REBUILT = (video_relpath(Aspect.WIDE), narration_relpath(Aspect.WIDE))


def _count_provider_calls(patch: pytest.MonkeyPatch) -> Counter:
    counts: Counter = Counter()
    for cls, method, label in PROVIDER_METHODS:
        original = getattr(cls, method)

        def counting(*args, _original=original, _label=label, **kwargs):
            counts[_label] += 1
            return _original(*args, **kwargs)

        patch.setattr(cls, method, counting)
    return counts


def _snapshot(root) -> dict:
    """Everything worth comparing either side of a `clean`, read in one go."""
    return {
        "build": sorted(p.name for p in (root / "build").glob("*")),
        "takes": sorted(p.name for p in root.glob("scenes/*/narration.wav")),
        "words": sorted(p.name for p in root.glob("scenes/*/words.json")),
        "assets": sorted(p.name for p in root.glob("scenes/*/asset.*")),
        "output": (root / output_relpath(Aspect.WIDE)).read_bytes(),
        "project_json": (root / "project.json").read_text(),
    }


def _stage_states(output: str) -> dict[str, str]:
    """`videomaker status`'s table, read back as `stage -> current|pending`."""
    states = {}
    for line in output.splitlines():
        found = re.search(r"\b(script|voice|align|visuals|captions|assemble|render)\b.*?"
                          r"\b(current|pending)\b", line)
        if found:
            states[found.group(1)] = found.group(2)
    return states


@pytest.fixture(scope="module")
def cleaned(tmp_path_factory):
    """Run, clean, look, run again — once, with everything worth asserting frozen."""
    tmp_path = tmp_path_factory.mktemp("clean_rerun")
    workspace = tmp_path / "workspace"
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)
        patch.setattr(runner_module, "USER_CACHE_DIR", tmp_path / "cache")

        built = runner.invoke(app, ["new", TOPIC, "-t", "tech_explainer", "-m", MINUTES])
        assert built.exit_code == 0, built.output
        first = runner.invoke(app, ["run", PROJECT_ID, "--providers", "mock", "--yes"])
        assert first.exit_code == 0, first.output

        root = ProjectStore(workspace).path_for(PROJECT_ID)
        before = _snapshot(root)

        cleaned_result = runner.invoke(app, ["clean", PROJECT_ID, "--yes"])
        # Read *now*: the re-run below legitimately rewrites `build/` and stamps a
        # fresh gate approval, so anything sampled afterwards says nothing about
        # what `clean` did.
        after = _snapshot(root)
        status_after = runner.invoke(app, ["status", PROJECT_ID])

        with pytest.MonkeyPatch.context() as inner:
            calls = _count_provider_calls(inner)
            second = runner.invoke(app, ["run", PROJECT_ID, "--providers", "mock", "--yes"])

        yield {
            "root": root,
            "before": before,
            "after": after,
            "clean": cleaned_result,
            "status": status_after,
            "rerun": second,
            "calls": calls,
        }


def test_clean_reported_what_it_removed(cleaned):
    assert cleaned["clean"].exit_code == 0, cleaned["clean"].output
    assert "build/" in cleaned["clean"].output
    assert "reclaimed" in cleaned["clean"].output


def test_the_build_intermediates_are_gone(cleaned):
    assert cleaned["before"]["build"], "the run should have produced intermediates to remove"
    assert cleaned["after"]["build"] == []


def test_the_deliverable_and_the_project_file_are_untouched(cleaned):
    assert cleaned["after"]["output"] == cleaned["before"]["output"]
    assert cleaned["after"]["project_json"] == cleaned["before"]["project_json"]


def test_the_takes_their_alignment_and_the_footage_are_untouched(cleaned):
    assert cleaned["before"]["takes"], "the run should have produced takes to protect"
    for kind in ("takes", "words", "assets"):
        assert cleaned["after"][kind] == cleaned["before"][kind]


def test_status_reports_assemble_pending_and_the_earlier_stages_current(cleaned):
    states = _stage_states(cleaned["status"].output)

    assert states["assemble"] == "pending"
    assert states["script"] == "current"
    assert states["voice"] == "current"
    assert states["align"] == "current"
    assert states["visuals"] == "current"


def test_the_rebuild_succeeds_and_restores_the_intermediates(cleaned):
    assert cleaned["rerun"].exit_code == 0, cleaned["rerun"].output
    for relpath in REBUILT:
        assert (cleaned["root"] / relpath).is_file()
    assert (cleaned["root"] / output_relpath(Aspect.WIDE)).is_file()


def test_the_rebuild_spends_no_provider_quota_at_all(cleaned):
    """Nothing re-scripted, re-voiced, re-aligned or re-downloaded."""
    assert cleaned["calls"] == Counter()
