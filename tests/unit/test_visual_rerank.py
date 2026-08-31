"""The optional Gemini Flash visual re-rank: the gate, the provider, the fallbacks.

Item 4 of `docs/visual-search-design.md`. Metadata ranking cannot see the picture,
so a vision model scoring the candidate *thumbnails* against the narration is the
one signal `pipeline.ranking` structurally cannot produce.

It is also the only part of the visuals stage that spends a **shared, daily**
budget: Gemini's soft budget is 240 requests a day and the script stage's LLM
fallback draws on the same one. Almost every test here is therefore about *not*
spending it — the gate, the off-by-default switch, the cache, and the four ways a
re-rank must fail back to the metadata order rather than into the pipeline.
"""

import json
from typing import ClassVar

import httpx
import pytest

from videomaker.cache import ResponseCache, StageCache
from videomaker.config import Settings, load_settings
from videomaker.models import Scene, SceneVisual, StockResult, VisualKind
from videomaker.pipeline.base import StageDeps
from videomaker.pipeline.ranking import (
    NARRATION_MATCH_CAP,
    NARRATION_WEIGHT,
    RERANK_MARGIN,
    RERANK_SCORE_FLOOR,
    is_ambiguous,
    scores_for,
)
from videomaker.pipeline.visuals import (
    MAX_CANDIDATES,
    RerankUnavailable,
    provider_names,
    run_visuals,
    scene_hash,
    vision_names,
)
from videomaker.project import ProjectStore
from videomaker.providers import register
from videomaker.providers.base import StockProvider, VisionProvider
from videomaker.providers.errors import (
    ProviderConfigError,
    ProviderResponseError,
    QuotaExceeded,
    TransientError,
)
from videomaker.providers.mock import MockStock
from videomaker.providers.ratelimit import SOFT_BUDGETS, QuotaTracker
from videomaker.providers.vision.gemini import (
    MAX_SCORE_TOKENS,
    MAX_SCORED_IMAGES,
    SCORE_JSON_OVERHEAD_TOKENS,
    SCORE_TOKENS_PER_IMAGE,
    GeminiVisionProvider,
)

QUERY = "nand flash cell macro"
NARRATION = "Every write wears the memory cells out a little more."
SCENE_DURATION_S = 4.0

CATALOGUE = {
    "models": [
        {
            "name": f"models/{GeminiVisionProvider.preference[0]}",
            "supportedGenerationMethods": ["generateContent"],
        }
    ]
}
JPEG_BYTES = b"\xff\xd8\xff\xe0 not really a jpeg, but bytes are bytes"


def _result(source_id: str, tags: list[str], *, preview: str | None = None) -> StockResult:
    return StockResult(
        provider="scripted",
        source_id=source_id,
        source_url=f"https://stock.invalid/{source_id}",
        preview_url=f"https://img.invalid/{source_id}.jpg" if preview is None else preview,
        download_url=f"https://stock.invalid/{source_id}.mp4",
        width=1920,
        height=1080,
        duration_s=8.0,
        tags=tags,
    )


#: A field the metadata ranker has a clear opinion about: one clip matches every
#: word of the query, the other matches none of it.
CONFIDENT = [_result("hit", ["nand", "flash", "cell", "macro"]), _result("miss", ["warehouse"])]
#: Two clips that match the query equally well. The ordering between them rests on
#: nothing but the provider's own listing order.
TIED = [_result("a", ["nand", "flash"]), _result("b", ["nand", "flash"])]
#: Nothing in the field matched much of anything.
THIN = [_result("weak", ["nand"]), _result("none", ["warehouse"])]


def _scores(results: list[StockResult]) -> list[float]:
    return scores_for(
        results, query=QUERY, narration=NARRATION, min_duration_s=SCENE_DURATION_S
    )


# ------------------------------------------------------------------------ the gate


def test_a_clear_metadata_winner_is_not_worth_a_request():
    """The gate is the whole point: 240 Gemini requests a day are shared with the LLM.

    A mutant that re-ranks every scene has to fail here.
    """
    assert is_ambiguous(_scores(CONFIDENT)) is False


def test_a_photo_finish_is_ambiguous():
    """Equal query overlap means the order came from the tie-breakers, not from evidence."""
    assert is_ambiguous(_scores(TIED)) is True


def test_a_weak_field_is_ambiguous_even_with_a_clear_leader():
    """Leading a field that understood nothing is not the same as being right."""
    scores = _scores(THIN)
    ranked = sorted(scores, reverse=True)
    assert ranked[0] < RERANK_SCORE_FLOOR
    # Not the margin doing the work: this field has a clear leader, it is just a bad one.
    assert ranked[0] - ranked[1] > RERANK_MARGIN
    assert is_ambiguous(scores) is True


def test_a_single_candidate_is_never_ambiguous():
    """There is no re-rank to buy when there is nothing to re-rank against."""
    assert is_ambiguous(_scores(CONFIDENT[:1])) is False
    assert is_ambiguous([]) is False


def test_the_margin_is_calibrated_to_one_narration_tag():
    """`RERANK_MARGIN` is a derived number, not a taste.

    One matching narration keyword is worth `NARRATION_WEIGHT / NARRATION_MATCH_CAP`.
    A lead smaller than that rests on resolution and headroom — tie-breakers that say
    nothing about what the clip shows. A lead of two tags is real evidence and must
    not fire.
    """
    one_tag = NARRATION_WEIGHT / NARRATION_MATCH_CAP
    assert one_tag < RERANK_MARGIN < 2 * one_tag


def test_the_floor_is_half_the_query():
    """Below `RERANK_SCORE_FLOOR` the leader has not matched half the words asked for."""
    assert is_ambiguous([RERANK_SCORE_FLOOR - 0.01, 0.0]) is True
    assert is_ambiguous([RERANK_SCORE_FLOOR + RERANK_MARGIN, 0.0]) is False


# -------------------------------------------------------------------- the provider


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _handler(scores, *, calls=None, image=JPEG_BYTES, image_status=200, mime="image/jpeg"):
    def handle(request):
        if calls is not None:
            calls.append(request)
        if request.url.host == "img.invalid":
            return httpx.Response(
                image_status, content=image, headers={"content-type": mime}
            )
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=CATALOGUE)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"text": json.dumps({"scores": scores})}]}}
                ]
            },
        )

    return handle


def _provider(handler, *, key="k", quota=None, cache=None):
    return GeminiVisionProvider(
        Settings(gemini_api_key=key), client=_client(handler), quota=quota, cache=cache
    )


URLS = ["https://img.invalid/a.jpg", "https://img.invalid/b.jpg"]


def test_scores_every_thumbnail_in_a_single_request():
    """One request per *scene*, not per image.

    The design doc budgets 10 scenes x 4 candidates = 40 images a video. Sent one at
    a time that is 40 requests against a 240/day soft budget — a sixth of the day for
    one video. Batched, a gated scene costs one.
    """
    calls: list[httpx.Request] = []
    provider = _provider(_handler([0.2, 0.9], calls=calls))

    assert provider.score_images(image_urls=URLS, query=QUERY, narration=NARRATION) == [0.2, 0.9]

    generate = [c for c in calls if c.url.path.endswith(":generateContent")]
    assert len(generate) == 1
    payload = json.loads(generate[0].content)
    inline = [p for p in payload["contents"][0]["parts"] if "inline_data" in p]
    assert len(inline) == len(URLS)


def test_scores_are_clamped_to_the_unit_interval():
    provider = _provider(_handler([5.0, -2.0]))
    assert provider.score_images(image_urls=URLS, query=QUERY, narration=NARRATION) == [1.0, 0.0]


def test_a_missing_key_never_reaches_the_network():
    calls: list[httpx.Request] = []
    provider = _provider(_handler([0.5, 0.5], calls=calls), key="")
    with pytest.raises(ProviderConfigError):
        provider.score_images(image_urls=URLS, query=QUERY, narration=NARRATION)
    assert calls == []


def test_the_request_is_recorded_and_persisted(tmp_path):
    """M2 found `record()` without `save()` loses the count.

    Gemini's budget is a *daily* one, and `build_deps` makes a fresh tracker per job,
    so a count that dies with the process can never enforce it.
    """
    path = tmp_path / "quota.json"
    provider = _provider(_handler([0.1, 0.2]), quota=QuotaTracker(path))
    provider.score_images(image_urls=URLS, query=QUERY, narration=NARRATION)

    reloaded = QuotaTracker(path)
    budget = SOFT_BUDGETS["gemini"]
    assert reloaded.remaining("gemini", budget)["per_day"] == budget.per_day - 1


def _spent(calls: list[httpx.Request]) -> list[httpx.Request]:
    """The calls that actually cost something: the scoring request and the thumbnails
    fetched only in order to make it. The `ListModels` catalogue lookup is neither —
    it is unbudgeted, cached for the process, and shared with the LLM provider."""
    return [
        call
        for call in calls
        if call.url.path.endswith(":generateContent") or call.url.host == "img.invalid"
    ]


def test_a_spent_budget_costs_no_request(tmp_path):
    quota = QuotaTracker(tmp_path / "quota.json")
    quota.record("gemini", SOFT_BUDGETS["gemini"].per_day or 0)
    calls: list[httpx.Request] = []
    provider = _provider(_handler([0.5, 0.5], calls=calls), quota=quota)

    with pytest.raises(QuotaExceeded):
        provider.score_images(image_urls=URLS, query=QUERY, narration=NARRATION)
    assert _spent(calls) == []


def test_a_cache_hit_costs_no_request_and_no_quota(tmp_path):
    cache = ResponseCache(tmp_path / "responses")
    quota = QuotaTracker(tmp_path / "quota.json")
    first = _provider(_handler([0.3, 0.8]), quota=quota, cache=cache)
    assert first.score_images(image_urls=URLS, query=QUERY, narration=NARRATION) == [0.3, 0.8]

    calls: list[httpx.Request] = []
    again = _provider(_handler([0.0, 0.0], calls=calls), quota=quota, cache=cache)
    assert again.score_images(image_urls=URLS, query=QUERY, narration=NARRATION) == [0.3, 0.8]
    assert _spent(calls) == []
    budget = SOFT_BUDGETS["gemini"]
    assert quota.remaining("gemini", budget)["per_day"] == budget.per_day - 1


def test_an_answer_of_the_wrong_length_is_a_response_error():
    provider = _provider(_handler([0.5]))
    with pytest.raises(ProviderResponseError):
        provider.score_images(image_urls=URLS, query=QUERY, narration=NARRATION)


def test_a_dead_thumbnail_is_a_transient_error():
    provider = _provider(_handler([0.5, 0.5], image_status=404))
    with pytest.raises(TransientError):
        provider.score_images(image_urls=URLS, query=QUERY, narration=NARRATION)


def test_a_thumbnail_that_is_not_an_image_is_rejected():
    provider = _provider(_handler([0.5, 0.5], mime="text/html"))
    with pytest.raises(ProviderResponseError):
        provider.score_images(image_urls=URLS, query=QUERY, narration=NARRATION)


def test_no_urls_is_answered_without_a_request():
    calls: list[httpx.Request] = []
    provider = _provider(_handler([], calls=calls))
    assert provider.score_images(image_urls=[], query=QUERY, narration=NARRATION) == []
    assert calls == []


# ------------------------------------------------------------------- the stage wiring


@register("stock", "scripted_stock")
class ScriptedStock(StockProvider):
    """Returns a fixed field of candidates, then downloads through the mock."""

    results: ClassVar[list[StockResult]] = []

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._mock = MockStock(settings)

    def search(self, *, query, kind, min_duration_s=0.0, orientation="landscape", per_page=4):
        return list(type(self).results)

    def download(self, result, out_path, *, max_height=1080):
        return self._mock.download(result, out_path, max_height=max_height)


@register("vision", "scripted_vision")
class ScriptedVision(VisionProvider):
    """A vision provider that either answers a scripted verdict or blows up."""

    scores: ClassVar[list[float]] = []
    asked: ClassVar[list[list[str]]] = []
    error: ClassVar[Exception | None] = None

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def score_images(self, *, image_urls, query, narration):
        type(self).asked.append(list(image_urls))
        if type(self).error is not None:
            raise type(self).error
        return list(type(self).scores)


@pytest.fixture
def vision():
    ScriptedVision.scores = []
    ScriptedVision.asked = []
    ScriptedVision.error = None
    yield ScriptedVision
    ScriptedVision.scores = []
    ScriptedVision.asked = []
    ScriptedVision.error = None


def _deps(tmp_path, *, enabled: bool, results: list[StockResult]):
    ScriptedStock.results = results
    settings = Settings(
        workspace_dir=tmp_path / "workspace",
        visual_rerank_enabled=enabled,
        provider_chains={
            "stock": ["scripted_stock"],
            "image": ["mock"],
            "vision": ["scripted_vision"],
        },
    )
    return StageDeps(
        settings=settings,
        store=ProjectStore(settings.workspace_dir),
        stage_cache=StageCache(tmp_path / "stages.json"),
        response_cache=ResponseCache(tmp_path / "responses"),
        quota=QuotaTracker(tmp_path / "quota.json"),
    )


def _project(deps):
    project = deps.store.create("how ssds work", "tech_explainer", target_minutes=1.0)
    project.scenes = [
        Scene(
            id="s01",
            narration=NARRATION,
            visual=SceneVisual(query=QUERY, kind=VisualKind.STOCK_VIDEO),
            audio_path="scenes/s01/narration.wav",
            duration_s=SCENE_DURATION_S,
        )
    ]
    deps.store.save(project)
    return project


def test_the_rerank_is_off_by_default(tmp_path, vision):
    """A feature that silently spends someone's daily cap is worse than one that does
    nothing. Default off until Task 14's harness shows it earns its quota."""
    assert Settings().visual_rerank_enabled is False
    deps = _deps(tmp_path, enabled=False, results=TIED)
    project = _project(deps)

    run_visuals(project, deps)

    assert vision.asked == []
    assert project.scenes[0].visual.chosen.source_id == "a"


def test_an_ambiguous_scene_is_rescored_and_reordered(tmp_path, vision):
    vision.scores = [0.1, 0.9]
    deps = _deps(tmp_path, enabled=True, results=TIED)
    project = _project(deps)

    run_visuals(project, deps)

    assert vision.asked == [[r.preview_url for r in TIED]]
    assert project.scenes[0].visual.chosen.source_id == "b"
    assert [c.source_id for c in project.scenes[0].visual.candidates] == ["b", "a"]


def test_a_confident_scene_costs_no_vision_request(tmp_path, vision):
    vision.scores = [0.0, 1.0]
    deps = _deps(tmp_path, enabled=True, results=CONFIDENT)
    project = _project(deps)

    run_visuals(project, deps)

    assert vision.asked == []
    assert project.scenes[0].visual.chosen.source_id == "hit"


def test_a_failing_vision_provider_leaves_the_metadata_order(tmp_path, vision):
    """No key, no quota, a bad response, a bug: never raise into the pipeline."""
    vision.error = RuntimeError("the model said something unparseable")
    deps = _deps(tmp_path, enabled=True, results=TIED)
    project = _project(deps)

    run_visuals(project, deps)

    assert vision.asked == [[r.preview_url for r in TIED]]
    assert project.scenes[0].visual.chosen.source_id == "a"


def test_an_unconfigured_vision_chain_leaves_the_metadata_order(tmp_path, vision):
    deps = _deps(tmp_path, enabled=True, results=TIED)
    deps.settings = deps.settings.model_copy(
        update={"provider_chains": {**deps.settings.provider_chains, "vision": []}}
    )
    project = _project(deps)

    run_visuals(project, deps)

    assert vision.asked == []
    assert project.scenes[0].visual.chosen.source_id == "a"


def test_a_wrong_length_verdict_is_ignored(tmp_path, vision):
    vision.scores = [0.9]
    deps = _deps(tmp_path, enabled=True, results=TIED)
    project = _project(deps)

    run_visuals(project, deps)

    assert project.scenes[0].visual.chosen.source_id == "a"


def test_candidates_without_a_thumbnail_are_never_reranked(tmp_path, vision):
    """`preview_url` is the only picture of a candidate that was never downloaded.

    A generated image, or any ref written before M3 Task 2, has none — and there is
    nothing to show a vision model.
    """
    vision.scores = [0.1, 0.9]
    blind = [_result("a", ["nand", "flash"], preview=""), _result("b", ["nand", "flash"])]
    deps = _deps(tmp_path, enabled=True, results=blind)
    project = _project(deps)

    run_visuals(project, deps)

    assert vision.asked == []
    assert project.scenes[0].visual.chosen.source_id == "a"


# -------------------------------------------------------------------- fingerprints


#: Captured from the pre-Task-13 tree. The owner has ten real projects on disk and
#: this codebase has been bitten three times by a stage hash moving under them.
PINNED_SCENE_HASH = "c2769661fbff128d"


def _hash_scene():
    scene = Scene(
        id="s01",
        narration="NAND cells wear out after enough writes.",
        visual=SceneVisual(query="flash memory chip macro"),
        duration_s=5.0,
    )
    kinds = [VisualKind.STOCK_VIDEO, VisualKind.STOCK_PHOTO]
    return scene, kinds


def test_the_scene_hash_does_not_move_while_the_rerank_is_off():
    scene, kinds = _hash_scene()
    assert scene_hash(scene, kinds, ["stock:mock"]) == PINNED_SCENE_HASH


def test_the_stage_composes_the_same_hash_it_did_before_task_13(tmp_path, vision):
    """The pin above only guards `scene_hash`. This guards what the stage feeds it —
    the half a mutant in `vision_names` would slip through."""
    scene, kinds = _hash_scene()
    deps = _deps(tmp_path, enabled=False, results=TIED)
    deps.settings = deps.settings.model_copy(
        update={"provider_chains": {"stock": ["mock"], "image": ["mock"], "vision": ["gemini"]}}
    )
    assert vision_names(deps) == []
    composed = scene_hash(scene, kinds, provider_names(deps, kinds) + vision_names(deps))
    assert composed == PINNED_SCENE_HASH


def test_enabling_the_rerank_stales_only_the_visuals_of_that_scene():
    """Turning it on *should* re-fetch — a different chosen shot is a different visual.

    It must do so through the per-scene `visuals:sNN` unit and nothing else: the
    vision provider is appended to the same `provider` list the stock chain already
    lives in, so no other stage's inputs can see it.
    """
    scene, kinds = _hash_scene()
    on = scene_hash(scene, kinds, ["stock:mock", "vision:gemini"])
    assert on != PINNED_SCENE_HASH


# ------------------------------------------------------------------------- config


def test_config_yaml_turns_the_rerank_on(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("visuals:\n  rerank_enabled: true\n")
    assert load_settings(path).visual_rerank_enabled is True


def test_config_yaml_without_a_visuals_section_leaves_it_off(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("paths:\n  workspace_dir: ./workspace\n")
    assert load_settings(path).visual_rerank_enabled is False


# ----------------------------------------------------------------- room to answer


def test_the_token_ceiling_fits_the_worst_case_this_stage_can_send():
    """`MAX_SCORE_TOKENS = 256` truncated the reply mid-array (M3 Task 14).

    The worst case is one score per candidate, `MAX_CANDIDATES` of them, plus the
    JSON around them. The ceiling is derived from exactly that — so a change to
    `MAX_CANDIDATES` cannot quietly outgrow it — and the rest is headroom, because
    Gemini 3.x bills its private reasoning against `maxOutputTokens` and a
    four-image comparison is precisely the prompt that provokes a long one.
    """
    answer = MAX_CANDIDATES * SCORE_TOKENS_PER_IMAGE + SCORE_JSON_OVERHEAD_TOKENS
    assert MAX_SCORED_IMAGES >= MAX_CANDIDATES  # the provider's copy of the bound
    assert MAX_SCORE_TOKENS >= answer
    # The observed truncation cut `{"scores": [0.65` off a 256-token budget: ~240
    # tokens went somewhere that was not the answer. Headroom below that is a
    # ceiling that has already been shown to fail.
    assert MAX_SCORE_TOKENS - answer > 256


def test_the_request_asks_for_the_whole_ceiling():
    calls: list[httpx.Request] = []
    provider = _provider(_handler([0.2, 0.9], calls=calls))
    provider.score_images(image_urls=URLS, query=QUERY, narration=NARRATION)

    generate = next(c for c in calls if c.url.path.endswith(":generateContent"))
    config = json.loads(generate.content)["generationConfig"]
    assert config["maxOutputTokens"] == MAX_SCORE_TOKENS


def _truncated_handler(text: str, finish: str = "MAX_TOKENS"):
    def handle(request):
        if request.url.host == "img.invalid":
            return httpx.Response(200, content=JPEG_BYTES, headers={"content-type": "image/jpeg"})
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=CATALOGUE)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"text": text}]}, "finishReason": finish}
                ]
            },
        )

    return handle


def test_a_reply_cut_mid_array_is_detected():
    """The literal body observed on 2026-08-31: `{"scores": [0.65`."""
    provider = _provider(_truncated_handler('{"scores": [0.65'))
    with pytest.raises(ProviderResponseError) as exc:
        provider.score_images(image_urls=URLS, query=QUERY, narration=NARRATION)
    assert "truncat" in str(exc.value).lower()


def test_a_reply_that_stops_early_but_parses_is_still_a_truncation():
    """The dangerous shape: valid JSON, a *short* list, `finishReason=MAX_TOKENS`.

    Detected by the finish reason, not by the length happening to disagree — a
    truncated answer must never be mistaken for a considered one.
    """
    provider = _provider(_truncated_handler('{"scores": [0.65]}'))
    with pytest.raises(ProviderResponseError) as exc:
        provider.score_images(image_urls=URLS, query=QUERY, narration=NARRATION)
    assert "truncat" in str(exc.value).lower()


def test_a_truncated_reply_still_costs_its_quota(tmp_path):
    """The model ran and produced tokens. That is spent capacity whatever came back."""
    ledger = tmp_path / "quota.json"
    provider = _provider(_truncated_handler('{"scores": [0.65'), quota=QuotaTracker(ledger))
    with pytest.raises(ProviderResponseError):
        provider.score_images(image_urls=URLS, query=QUERY, narration=NARRATION)

    budget = SOFT_BUDGETS["gemini"]
    assert QuotaTracker(ledger).remaining("gemini", budget)["per_day"] == budget.per_day - 1


# -------------------------------------------------- a model the account cannot call


def _vision_retiring_handler(dead: set[str], calls: list[httpx.Request] | None = None):
    listed = {
        "models": [
            {"name": f"models/{m}", "supportedGenerationMethods": ["generateContent"]}
            for m in GeminiVisionProvider.preference
        ]
    }

    def handle(request):
        if calls is not None:
            calls.append(request)
        if request.url.host == "img.invalid":
            return httpx.Response(200, content=JPEG_BYTES, headers={"content-type": "image/jpeg"})
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=listed)
        model = request.url.path.rsplit("/", 1)[-1].split(":")[0]
        if model in dead:
            return httpx.Response(
                404,
                json={
                    "error": {
                        "code": 404,
                        "status": "NOT_FOUND",
                        "message": f"This model models/{model} is no longer available to new users.",
                    }
                },
            )
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": json.dumps({"scores": [0.4, 0.6]})}]}}]},
        )

    return handle


def test_a_retired_vision_model_falls_through_and_answers():
    provider = _provider(_vision_retiring_handler({GeminiVisionProvider.preference[0]}))
    assert provider.score_images(image_urls=URLS, query=QUERY, narration=NARRATION) == [0.4, 0.6]


def test_a_retired_vision_model_books_no_quota(tmp_path):
    """240 units on 404s is what this test exists to make impossible.

    The refused request never reached a model, so it is not spent capacity. The
    retry that *did* answer costs one — not two.
    """
    ledger = tmp_path / "quota.json"
    provider = _provider(
        _vision_retiring_handler({GeminiVisionProvider.preference[0]}), quota=QuotaTracker(ledger)
    )
    provider.score_images(image_urls=URLS, query=QUERY, narration=NARRATION)

    budget = SOFT_BUDGETS["gemini"]
    assert QuotaTracker(ledger).remaining("gemini", budget)["per_day"] == budget.per_day - 1


def test_a_wholly_dead_preference_list_books_nothing(tmp_path):
    ledger = tmp_path / "quota.json"
    provider = _provider(
        _vision_retiring_handler(set(GeminiVisionProvider.preference)), quota=QuotaTracker(ledger)
    )
    with pytest.raises(ProviderConfigError):
        provider.score_images(image_urls=URLS, query=QUERY, narration=NARRATION)

    budget = SOFT_BUDGETS["gemini"]
    assert QuotaTracker(ledger).remaining("gemini", budget)["per_day"] == budget.per_day


# ------------------------------------------------------------ the failure is visible


def test_a_failed_rerank_leaves_a_trace_in_the_stage_result(tmp_path, vision):
    """The actual M3 Task 14 bug: a provider that never answered once was
    indistinguishable from one that worked, for a hundred scenes.

    The fallback policy is right — a re-rank may never fail a scene — but it must
    not be silent. The stage reports it and the CLI prints it.
    """
    vision.error = RuntimeError("404 no longer available to new users")
    deps = _deps(tmp_path, enabled=True, results=TIED)
    project = _project(deps)

    with pytest.warns(RerankUnavailable):
        result = run_visuals(project, deps)

    assert project.scenes[0].visual.chosen.source_id == "a"  # policy unchanged
    assert len(result.warnings) == 1
    note = result.warnings[0]
    assert "s01" in note
    assert "no longer available" in note


def test_a_working_rerank_leaves_no_trace(tmp_path, vision):
    """A mutant that warns unconditionally has to fail here."""
    vision.scores = [0.1, 0.9]
    deps = _deps(tmp_path, enabled=True, results=TIED)
    project = _project(deps)

    result = run_visuals(project, deps)

    assert project.scenes[0].visual.chosen.source_id == "b"
    assert result.warnings == ()


def test_a_scene_the_gate_never_opened_leaves_no_trace(tmp_path, vision):
    """Not spending a request is not a failure, and must not read like one."""
    deps = _deps(tmp_path, enabled=True, results=CONFIDENT)
    project = _project(deps)

    result = run_visuals(project, deps)

    assert vision.asked == []
    assert result.warnings == ()


def test_a_rerank_failure_is_not_recorded_as_a_scene_error(tmp_path, vision):
    """`Scene.error` means *this scene failed*; the storyboard renders it as broken.

    A re-rank that fell back produced a perfectly good scene, so the trace belongs
    in the run report, not on the artefact.
    """
    vision.error = RuntimeError("boom")
    deps = _deps(tmp_path, enabled=True, results=TIED)
    project = _project(deps)

    with pytest.warns(RerankUnavailable):
        run_visuals(project, deps)

    assert project.scenes[0].error is None
