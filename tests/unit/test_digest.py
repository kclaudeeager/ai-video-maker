"""The brief: one call, one repair retry, then the next provider — and a cache
keyed so a prompt change cannot serve an answer written to the old question.

The brief is a retelling. `corpus/digest.py` is the only module in the reader that
puts a model between the reader and the text, which is why the label is a feature
of every surface that shows one and why `listen` never comes near this module.
"""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from videomaker import cli as cli_module
from videomaker import runner as runner_module
from videomaker.cli import app
from videomaker.config import Settings
from videomaker.corpus import digest as digest_module
from videomaker.corpus import importer
from videomaker.corpus.audio import reader_deps
from videomaker.corpus.catalogue import CATALOGUE
from videomaker.corpus.digest import (
    MAX_SUMMARY_WORDS,
    PROMPT_VERSION,
    Brief,
    brief_key,
    brief_path,
    build_brief,
    build_prompt,
    parse_brief,
)
from videomaker.corpus.importer import import_work
from videomaker.corpus.models import UnitRef, UnitText, Verse
from videomaker.pipeline.base import StageDeps
from videomaker.providers.base import LLMProvider, LLMResult
from videomaker.providers.errors import ProviderError, ProviderResponseError, QuotaExceeded
from videomaker.providers.ratelimit import SOFT_BUDGETS, Budget, QuotaTracker
from videomaker.runner import PROVIDER_KINDS

GOOD = {
    "summary": "A short retelling of the passage in plain words.",
    "people": ["Someone"],
    "places": ["A town"],
    "turn": "The reader is told what changes.",
}


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        workspace_dir=tmp_path / "workspace",
        provider_chains={kind: ["mock"] for kind in PROVIDER_KINDS},
    )


@pytest.fixture
def deps(settings, tmp_path) -> StageDeps:
    return reader_deps(settings, cache_dir=tmp_path / "cache")


@pytest.fixture
def unit(deps) -> UnitText:
    return deps.provider("corpus").unit(UnitRef(work_id="mock", book="JHN", chapter=1))


class Scripted(LLMProvider):
    """An LLM that answers from a list, recording every prompt it was given."""

    def __init__(self, *replies: str, fail: Exception | None = None) -> None:
        self.replies = list(replies)
        self.fail = fail
        self.prompts: list[str] = []

    def generate(self, *, system, user, json_schema=None, temperature=0.7, max_tokens=2048):
        self.prompts.append(user)
        if self.fail is not None:
            raise self.fail
        return LLMResult(text=self.replies.pop(0), model="scripted-1")


def chain(deps: StageDeps, **providers: LLMProvider) -> None:
    """Point the `llm` chain at these named fakes, best first."""
    deps.settings = deps.settings.model_copy(
        update={"provider_chains": {**deps.settings.provider_chains, "llm": list(providers)}}
    )
    for name, provider in providers.items():
        deps.instances[("llm", name)] = provider


# ------------------------------------------------------------------ the prompt


def test_the_prompt_carries_the_passage_and_forbids_adding_to_it(unit):
    system, user = build_prompt(unit)
    assert unit.plain in user
    assert unit.title in user
    assert "traceable to the passage" in system
    assert str(MAX_SUMMARY_WORDS) in system


# ---------------------------------------------------------------- validation


def test_a_summary_over_the_limit_fails_validation():
    too_long = {**GOOD, "summary": " ".join(["word"] * (MAX_SUMMARY_WORDS + 1))}
    with pytest.raises(ProviderResponseError, match="limit"):
        parse_brief(json.dumps(too_long), ref_key="w/JHN/001", model="m")
    just_short = {**GOOD, "summary": " ".join(["word"] * MAX_SUMMARY_WORDS)}
    assert parse_brief(json.dumps(just_short), ref_key="w/JHN/001", model="m").summary


def test_a_reply_wrapped_in_prose_or_a_fence_still_parses():
    fenced = f"Here you go:\n```json\n{json.dumps(GOOD)}\n```"
    brief = parse_brief(fenced, ref_key="w/JHN/001", model="m")
    assert brief.summary == GOOD["summary"]
    assert brief.ref_key == "w/JHN/001"
    assert brief.model == "m"


def test_an_unusable_reply_is_a_response_error():
    with pytest.raises(ProviderResponseError):
        parse_brief("no json here", ref_key="w/JHN/001", model="m")


# ------------------------------------------------------------- the repair retry


def test_a_reply_failing_validation_earns_exactly_one_retry(deps, unit):
    llm = Scripted("not json at all", json.dumps(GOOD))
    chain(deps, only=llm)
    brief = build_brief(unit, deps)
    assert brief.summary == GOOD["summary"]
    assert len(llm.prompts) == 2
    # The retry carries the validation error and the bad reply back to the model.
    assert "could not be used" in llm.prompts[1]
    assert "not json at all" in llm.prompts[1]


def test_two_bad_replies_advance_the_chain(deps, unit):
    first = Scripted("rubbish", "still rubbish")
    second = Scripted(json.dumps(GOOD))
    chain(deps, first=first, second=second)
    brief = build_brief(unit, deps)
    assert brief.summary == GOOD["summary"]
    assert len(first.prompts) == 2
    assert len(second.prompts) == 1


def test_a_spent_quota_advances_the_chain_too(deps, unit):
    spent = Scripted(fail=QuotaExceeded("no units left today"))
    fallback = Scripted(json.dumps(GOOD))
    chain(deps, spent=spent, fallback=fallback)
    assert build_brief(unit, deps).summary == GOOD["summary"]


def test_every_provider_failing_raises(deps, unit):
    chain(deps, a=Scripted("rubbish", "rubbish"), b=Scripted("rubbish", "rubbish"))
    with pytest.raises(ProviderError):
        build_brief(unit, deps)


# ---------------------------------------------------------------------- cache


def test_a_second_build_makes_no_call(deps, unit):
    llm = Scripted(json.dumps(GOOD))
    chain(deps, only=llm)
    first = build_brief(unit, deps)
    second = build_brief(unit, deps)
    assert second == first
    assert len(llm.prompts) == 1


def test_the_cache_is_not_re_read_across_a_prompt_version_bump(deps, unit, settings):
    llm = Scripted(json.dumps(GOOD))
    chain(deps, only=llm)
    build_brief(unit, deps)
    written = brief_path(settings.workspace_dir, unit, brief_key(unit, model="only"))
    assert written.is_file()

    bumped = brief_key(unit, model="only", prompt_version=PROMPT_VERSION + 1)
    assert not brief_path(settings.workspace_dir, unit, bumped).is_file()
    assert bumped != brief_key(unit, model="only")


def test_the_key_covers_the_text_and_the_model(deps):
    one = UnitText(
        ref=UnitRef(work_id="w", book="JHN", chapter=1),
        title="John 1",
        verses=[Verse(number=1, text="First.")],
    )
    other = UnitText(
        ref=one.ref, title=one.title, verses=[Verse(number=1, text="Different words.")]
    )
    assert brief_key(one, model="a") != brief_key(other, model="a")
    assert brief_key(one, model="a") != brief_key(one, model="b")


def test_a_cached_brief_of_an_older_shape_is_replaced_rather_than_raising(
    deps, unit, settings
):
    llm = Scripted(json.dumps(GOOD))
    chain(deps, only=llm)
    path = brief_path(settings.workspace_dir, unit, brief_key(unit, model="only"))
    path.parent.mkdir(parents=True)
    path.write_text('{"summary": "written by an older build", "gone": true}')
    brief = build_brief(unit, deps)
    assert brief.summary == GOOD["summary"]
    assert Brief.model_validate_json(path.read_text()) == brief


# ------------------------------------------------------------------- bulk CLI


runner = CliRunner()


@pytest.fixture
def library(tmp_path, monkeypatch):
    """A real two-book import in a cwd-relative workspace, as the CLI sees it."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(importer, "NOTICE_PATH", tmp_path / "NOTICE.md")
    monkeypatch.setattr(runner_module, "USER_CACHE_DIR", tmp_path / "cache")
    fixtures = Path(__file__).resolve().parents[1] / "fixtures" / "usfm"
    import_work(CATALOGUE["fixture"].model_copy(update={"archive": str(fixtures)}), Path("workspace"))
    return tmp_path


def brief_files(root: Path) -> list[Path]:
    return sorted((root / "workspace" / "library" / "fixture" / "derived" / "brief").glob("*.json"))


def test_the_bulk_command_writes_one_brief_per_chapter_and_says_n_of_total(library):
    result = runner.invoke(app, ["library", "brief", "fixture", "--book", "JHN", "--providers", "mock"])
    assert result.exit_code == 0, result.output
    assert "1/3" in result.output and "3/3" in result.output
    assert "3 written" in result.output
    assert len(brief_files(library)) == 3


def test_re_running_skips_what_is_cached_and_continues(library):
    runner.invoke(app, ["library", "brief", "fixture", "--book", "JHN", "--providers", "mock"])
    result = runner.invoke(app, ["library", "brief", "fixture", "--providers", "mock"])
    assert result.exit_code == 0, result.output
    assert "3 written, 3 already cached" in result.output
    assert len(brief_files(library)) == 6


def test_the_bulk_command_keeps_the_real_library_under_providers_mock(library):
    # `--providers mock` is about not reaching the network; the library is already
    # on this disk, and overriding `corpus` too would brief the mock work instead.
    result = runner.invoke(app, ["library", "brief", "fixture", "--providers", "mock"])
    assert result.exit_code == 0, result.output
    assert "fixture/GEN/001" in result.output


def test_an_unknown_book_fails_naming_it(library):
    result = runner.invoke(app, ["library", "brief", "fixture", "--book", "REV", "--providers", "mock"])
    assert result.exit_code == 1
    assert "REV" in result.output


def test_a_spent_daily_cap_stops_cleanly_with_the_count_and_the_reset(library, monkeypatch):
    """Not an error: the work stopped, as designed, and re-running continues it."""
    calls = {"n": 0}
    real = digest_module.build_brief

    def spend_after_two(unit, deps):
        calls["n"] += 1
        if calls["n"] > 2:
            raise QuotaExceeded("gemini daily free-tier cap reached")
        return real(unit, deps)

    monkeypatch.setattr(digest_module, "build_brief", spend_after_two)
    result = runner.invoke(app, ["library", "brief", "fixture", "--providers", "mock"])
    assert result.exit_code == 0, result.output
    assert "stopped" in result.output
    assert "3/6" in result.output
    assert "2 written" in result.output
    assert "resets in" in result.output
    assert len(brief_files(library)) == 2

    monkeypatch.setattr(digest_module, "build_brief", real)
    again = runner.invoke(app, ["library", "brief", "fixture", "--providers", "mock"])
    assert "4 written, 2 already cached" in again.output


def test_the_command_paces_against_the_shared_quota_ledger(library, monkeypatch):
    """A per-minute cap is a client-side sleep, not a rejection to catch."""
    slept: list[float] = []
    monkeypatch.setattr(cli_module.time, "sleep", slept.append)
    monkeypatch.setattr(
        QuotaTracker, "wait_s", lambda self, provider, budget: 4.0 if provider == "mock" else 0.0
    )
    monkeypatch.setitem(SOFT_BUDGETS, "mock", Budget(rpm=1))
    result = runner.invoke(app, ["library", "brief", "fixture", "--book", "GEN", "--providers", "mock"])
    assert result.exit_code == 0, result.output
    assert slept == [4.0, 4.0]
    assert "pacing" in result.output
