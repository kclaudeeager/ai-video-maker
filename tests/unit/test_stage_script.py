"""The script stage: schema-guided generation, one repair retry, chain fallback."""

import pytest

from videomaker.cache import ResponseCache, StageCache, stage_key
from videomaker.config import Settings
from videomaker.pipeline.base import StageDeps
from videomaker.pipeline.script import run_script
from videomaker.project import ProjectStore
from videomaker.providers import register
from videomaker.providers.base import LLMProvider, LLMResult
from videomaker.providers.errors import ProviderError
from videomaker.providers.mock import MockLLM
from videomaker.providers.ratelimit import QuotaTracker

TEMPLATE = "tech_explainer"
# 0.8 min x 150 wpm / 30 words-per-scene = 4, which is tech_explainer's minimum.
TARGET_MINUTES = 0.8
EXPECTED_SCENES = 4

#: Per-test call counts, keyed by provider name; reset by the autouse fixture.
CALLS: dict[str, int] = {}


def _count(name: str) -> int:
    CALLS[name] = CALLS.get(name, 0) + 1
    return CALLS[name]


@register("llm", "counting_mock")
class CountingMockLLM(MockLLM):
    """`mock`, but it says how many times it was asked."""

    def generate(self, **kwargs) -> LLMResult:
        _count("counting_mock")
        return super().generate(**kwargs)


@register("llm", "malformed_once")
class MalformedOnceLLM(LLMProvider):
    """Prose on the first call, a conforming payload once the repair prompt arrives."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def generate(self, **kwargs) -> LLMResult:
        if _count("malformed_once") == 1:
            return LLMResult(text="Sure! Here is your script, hope it helps.", model="broken-v1")
        return MockLLM(self.settings).generate(**kwargs)


@register("llm", "always_malformed")
class AlwaysMalformedLLM(LLMProvider):
    """Valid JSON that never validates — the repair retry cannot save it."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def generate(self, **kwargs) -> LLMResult:
        _count("always_malformed")
        return LLMResult(text='{"scenes": "not a list of scenes"}', model="broken-v2")


@pytest.fixture(autouse=True)
def _reset_calls():
    CALLS.clear()
    yield
    CALLS.clear()


@pytest.fixture
def deps_for(tmp_path):
    def build(*chain: str) -> StageDeps:
        settings = Settings(
            workspace_dir=tmp_path / "workspace",
            provider_chains={"llm": list(chain)},
        )
        return StageDeps(
            settings=settings,
            store=ProjectStore(settings.workspace_dir),
            stage_cache=StageCache(tmp_path / "stages.json"),
            response_cache=ResponseCache(tmp_path / "responses"),
            quota=QuotaTracker(tmp_path / "quota.json"),
        )

    return build


def _project(deps: StageDeps):
    return deps.store.create("how ssds work", TEMPLATE, target_minutes=TARGET_MINUTES)


def test_script_fills_the_project_with_stable_scene_ids(deps_for):
    deps = deps_for("mock")
    project = _project(deps)

    result = run_script(project, deps)

    assert result.changed is True
    assert [scene.id for scene in project.scenes] == ["s01", "s02", "s03", "s04"]
    assert len(project.scenes) == EXPECTED_SCENES


def test_script_writes_narration_and_a_visual_query_for_every_scene(deps_for):
    deps = deps_for("mock")
    project = _project(deps)

    run_script(project, deps)

    for scene in project.scenes:
        assert scene.narration.strip()
        assert scene.visual.query.strip()
        assert scene.audio_path is None  # the script stage voices nothing


def test_script_persists_the_project_and_the_stage_cache(deps_for):
    """A later process must see both the scenes and the "already done" mark."""
    deps = deps_for("counting_mock")
    project = _project(deps)
    run_script(project, deps)

    next_run = deps_for("counting_mock")
    reloaded = next_run.store.load(project.id)
    assert [scene.narration for scene in reloaded.scenes] == [
        scene.narration for scene in project.scenes
    ]
    assert next_run.stage_cache.is_stale(stage_key("script"), "anything") is True

    result = run_script(reloaded, next_run)

    assert CALLS["counting_mock"] == 1  # the stage cache survived the "restart"
    assert result.changed is False


def test_clean_rerun_makes_zero_llm_calls(deps_for):
    deps = deps_for("counting_mock")
    project = _project(deps)

    run_script(project, deps)
    assert CALLS["counting_mock"] == 1

    again = run_script(project, deps)

    assert CALLS["counting_mock"] == 1  # not one more
    assert again.changed is False
    assert again.skipped_units == 1


def test_editing_the_topic_invalidates_the_script(deps_for):
    deps = deps_for("counting_mock")
    project = _project(deps)
    run_script(project, deps)

    project.topic = "how nand flash wears out"
    result = run_script(project, deps)

    assert CALLS["counting_mock"] == 2
    assert result.changed is True


def test_malformed_response_triggers_exactly_one_repair_retry(deps_for):
    deps = deps_for("malformed_once")
    project = _project(deps)

    result = run_script(project, deps)

    assert CALLS["malformed_once"] == 2  # the original call plus one repair
    assert result.changed is True
    assert len(project.scenes) == EXPECTED_SCENES


def test_persistently_malformed_provider_advances_to_the_next_in_the_chain(deps_for):
    deps = deps_for("always_malformed", "counting_mock")
    project = _project(deps)

    result = run_script(project, deps)

    assert CALLS["always_malformed"] == 2  # tried, repaired once, then given up on
    assert CALLS["counting_mock"] == 1  # the fallback produced the script
    assert result.changed is True
    assert len(project.scenes) == EXPECTED_SCENES


def test_whole_chain_failing_raises_rather_than_writing_a_broken_script(deps_for):
    deps = deps_for("always_malformed")
    project = _project(deps)

    with pytest.raises(ProviderError) as exc:
        run_script(project, deps)

    assert "always_malformed" in str(exc.value)
    assert project.scenes == []
