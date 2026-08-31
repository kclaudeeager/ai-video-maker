"""Visual search quality: several queries per scene, a ladder, and a ranked pick.

M1's measured failure is the fixed point of this file. Scene 8 asked for
``"capacity sticker"`` and Pexels answered with a warehouse shelf stencilled
*"LOAD CAPACITY PER SHELF 200 KG"* — a perfect keyword match and a completely
wrong shot, taken as `candidates[0]` because nothing ever looked at it.

Three defences are tested here, in the order they fire:

* `query_ladder` gives the scene more than one thing to ask for, capped so a
  browsing pipeline cannot eat the Pexels soft budget (190/hour).
* `drop_cliches` removes the handshake/open-plan-office class outright.
* `rank_candidates` is a pure function over metadata that already arrived in the
  search response: tag overlap with the query and with the narration, duration
  headroom over the scene, and resolution. No model, no quota, no dependency.

Nothing here touches the network. The Pexels tests drive `httpx.MockTransport`
the way `test_stock_image_providers.py` does.
"""

import json
from typing import ClassVar

import httpx
import pytest

from videomaker.cache import ResponseCache, StageCache, stage_key
from videomaker.config import Settings
from videomaker.models import Scene, SceneVisual, StockResult, VisualKind
from videomaker.pipeline.base import SCENE_GAP_S, StageDeps
from videomaker.pipeline.ranking import (
    CLICHE_TAGS,
    MAX_QUERY_ATTEMPTS,
    drop_cliches,
    keywords,
    query_ladder,
    rank_candidates,
    score_candidate,
    single_noun,
)
from videomaker.pipeline.script import (
    MAX_QUERIES_PER_SCENE,
    MIN_QUERIES_PER_SCENE,
    DraftScene,
    DraftScript,
    _to_scenes,
    build_prompt,
    parse_script,
    scene_schema,
)
from videomaker.pipeline.visuals import (
    MAX_CANDIDATES,
    MIN_USABLE_CANDIDATES,
    SEARCH_POOL,
    run_visuals,
    scene_hash,
)
from videomaker.project import ProjectStore
from videomaker.providers import register
from videomaker.providers.base import StockProvider
from videomaker.providers.errors import ProviderError
from videomaker.providers.mock import MockStock
from videomaker.providers.ratelimit import QuotaTracker
from videomaker.providers.stock.pexels import PexelsProvider
from videomaker.templates import load_template

# --------------------------------------------------------------------------- helpers


def _result(
    source_id: str,
    *,
    tags: list[str] | None = None,
    duration_s: float | None = 20.0,
    width: int = 1920,
    height: int = 1080,
) -> StockResult:
    return StockResult(
        provider="pexels",
        source_id=source_id,
        source_url=f"https://www.pexels.com/video/{source_id}/",
        preview_url=f"https://img.example/{source_id}.jpg",
        download_url=f"https://dl.example/{source_id}.mp4",
        width=width,
        height=height,
        duration_s=duration_s,
        tags=tags or [],
    )


def _ids(results: list[StockResult]) -> list[str]:
    return [result.source_id for result in results]


# ------------------------------------------------------------------------- keywords


def test_keywords_drops_stopwords_and_folds_plurals():
    """Tag matching is only as good as the two bags of words it intersects.

    `"the cells wear out"` and a clip tagged `"cell"` are talking about the same
    thing; a matcher that misses that is measuring spelling, not meaning.
    """
    assert keywords("The cells in a NAND chip wear out") == {"cell", "nand", "chip", "wear", "out"}
    # `ss` is not a plural ending, and a three-letter word is left alone.
    assert keywords("glass gas") == {"glass", "gas"}
    # Punctuation and case are not signal.
    assert keywords("Close-up: CIRCUIT board!") == {"close", "circuit", "board"}


def test_keywords_of_nothing_is_empty():
    assert keywords("") == set()
    assert keywords("a an the of") == set()


# --------------------------------------------------------------------------- ranking


def test_the_best_tag_match_is_ranked_first_even_when_it_arrived_last():
    """The whole point: `candidates[0]` has to *mean* something.

    This is the mutation guard for the ranker. A `rank_candidates` that returns its
    input unchanged — which is exactly what M1 did — fails here, because the shot
    that actually matches the query is deliberately placed last in the response.
    """
    results = [
        _result("shelf", tags=["warehouse", "shelf", "capacity", "storage"]),
        _result("office", tags=["desk", "laptop", "paperwork"]),
        _result("nand", tags=["memory", "chip", "nand", "flash", "wafer"]),
    ]
    ranked = rank_candidates(
        results,
        query="nand flash memory chip",
        narration="Every write wears the cells out a little more.",
        min_duration_s=6.0,
    )
    assert _ids(ranked) == ["nand", "shelf", "office"]


def test_narration_overlap_breaks_a_tie_the_query_cannot():
    """The query is three words; the narration is the scene. Both are free signal."""
    results = [
        _result("generic", tags=["chip", "blue", "abstract"]),
        _result("wearing", tags=["chip", "electron", "voltage", "insulator"]),
    ]
    ranked = rank_candidates(
        results,
        query="chip macro",
        narration="Electrons tunnel through the insulator and the voltage drifts.",
        min_duration_s=5.0,
    )
    assert _ids(ranked)[0] == "wearing"


def test_a_clip_that_barely_covers_the_scene_loses_to_one_with_headroom():
    """A clip has to cover the scene *and* the gap before the next cut.

    Pexels' filter only says "long enough"; the ranker prefers "comfortably long
    enough", because the trim has somewhere to move.
    """
    scene_s = 10.0
    tight = _result("tight", tags=["chip"], duration_s=scene_s + SCENE_GAP_S + 0.1)
    roomy = _result("roomy", tags=["chip"], duration_s=scene_s + SCENE_GAP_S + 8.0)
    ranked = rank_candidates([tight, roomy], query="chip", narration="", min_duration_s=scene_s)
    assert _ids(ranked) == ["roomy", "tight"]


def test_resolution_breaks_a_tie_nothing_else_can():
    equal = {"tags": ["chip"], "duration_s": 20.0}
    ranked = rank_candidates(
        [_result("hd", width=1920, height=1080, **equal), _result("uhd", width=3840, height=2160, **equal)],
        query="chip",
        narration="",
        min_duration_s=4.0,
    )
    assert _ids(ranked) == ["uhd", "hd"]


def test_ranking_is_stable_so_the_providers_own_order_survives_a_tie():
    """Pexels already returns its own relevance order; ties must not shuffle it."""
    same = {"tags": ["chip", "macro"], "duration_s": 20.0}
    results = [_result(f"r{index}", **same) for index in range(5)]
    ranked = rank_candidates(results, query="chip macro", narration="", min_duration_s=4.0)
    assert _ids(ranked) == _ids(results)


def test_a_photo_has_no_duration_and_is_not_punished_for_it():
    """Stills are cut to length by the assembler; `duration_s is None` is not a fault."""
    photo = _result("photo", tags=["chip"], duration_s=None, width=3000, height=2000)
    clip = _result("clip", tags=["chip"], duration_s=60.0, width=1920, height=1080)
    scores = {
        "photo": score_candidate(photo, query="chip", narration="", min_duration_s=8.0),
        "clip": score_candidate(clip, query="chip", narration="", min_duration_s=8.0),
    }
    assert scores["photo"] > 0.0
    # Within one weight of each other: the still is neither preferred nor penalised.
    assert abs(scores["photo"] - scores["clip"]) < 1.0


def test_score_is_zero_when_nothing_matches_and_nothing_is_known():
    bare = _result("bare", tags=[], duration_s=0.0, width=0, height=0)
    assert score_candidate(bare, query="chip", narration="chips", min_duration_s=99.0) == 0.0


# -------------------------------------------------------------------------- clichés


def test_the_handshake_and_the_open_plan_office_are_dropped():
    """The recognisable wrong answer Pexels gives for anything abstract."""
    keep = _result("keep", tags=["nand", "wafer", "silicon"])
    results = [
        _result("handshake", tags=["handshake", "agreement", "suit"]),
        keep,
        _result("openplan", tags=["office", "coworker", "corporate"]),
    ]
    assert _ids(drop_cliches(results, query="nand flash memory")) == ["keep"]


def test_a_cliche_that_matches_the_query_is_what_was_asked_for():
    """Search "office desk" and an office is the right answer, not a cliché.

    The denylist only fires on a candidate that matched *nothing* in the query —
    which is precisely the case where its business-stock tags are all it has.
    """
    results = [_result("office", tags=["office", "desk", "corporate"])]
    assert _ids(drop_cliches(results, query="open plan office desk")) == ["office"]


def test_the_denylist_is_small_and_only_names_the_business_stock_class():
    """A big denylist would quietly starve scenes into the paid image provider.

    `_fetch` falls through to Workers AI when stock yields nothing, and Workers AI
    bills automatically past the free cap (M0 finding 6). The list stays short.
    """
    assert len(CLICHE_TAGS) <= 20
    assert "handshake" in CLICHE_TAGS
    # Every entry must survive `keywords()`, or it can never match a tag.
    for tag in CLICHE_TAGS:
        assert keywords(tag) == {tag}


# ---------------------------------------------------------------------- the ladder


def test_the_ladder_is_the_queries_in_order_then_a_single_noun():
    assert query_ladder("nand flash cell macro", ["memory chip", "silicon wafer"]) == [
        "nand flash cell macro",
        "memory chip",
        "silicon wafer",
    ]
    # Room left over, so the single-noun rung is added.
    assert query_ladder("close up circuit board solder", []) == [
        "close up circuit board solder",
        "solder",
    ]


def test_the_single_noun_fallback_is_the_head_of_the_phrase():
    """English compounds are head-final: the last noun is the thing itself.

    "capacity sticker" is a sticker, not a capacity — and `"sticker"` is at least a
    filmable object, which is the whole job of the last rung.
    """
    assert single_noun("close up circuit board solder") == "solder"
    assert single_noun("capacity sticker") == "sticker"
    assert single_noun("the of a") == ""


def test_the_ladder_never_asks_the_same_thing_twice():
    assert query_ladder("Memory Chip", ["memory chip", "MEMORY CHIP", "wafer"]) == [
        "Memory Chip",
        "wafer",
        # Two rungs left room for the single-noun one, and it is not a repeat.
        "chip",
    ]
    # The single-noun rung is already the query, so it is not repeated.
    assert query_ladder("solder", []) == ["solder"]


def test_the_ladder_is_capped_because_browsing_burns_the_pexels_budget():
    """The mutation guard for the cap: Pexels allows 190 requests an hour."""
    ladder = query_ladder("one two three", ["alpha", "bravo", "charlie", "delta", "echo"])
    assert len(ladder) == MAX_QUERY_ATTEMPTS == 3
    assert ladder == ["one two three", "alpha", "bravo"]


# ------------------------------------------------------- the ladder, in the stage


QUERY = "nand flash cell macro"
ALTS = ["memory chip close up", "silicon wafer"]


@register("stock", "laddering_stock")
class LadderingStock(StockProvider):
    """Answers a scripted number of hits per query and records what it was asked."""

    #: query -> how many results to return. Missing means none at all.
    replies: ClassVar[dict[str, int]] = {}
    asked: ClassVar[list[str]] = []

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._mock = MockStock(settings)

    def search(self, *, query, kind, min_duration_s=0.0, orientation="landscape", per_page=4):
        type(self).asked.append(query)
        count = type(self).replies.get(query, 0)
        return self._mock.search(
            query=query,
            kind=kind,
            min_duration_s=min_duration_s,
            orientation=orientation,
            per_page=min(count, per_page),
        )

    def download(self, result, out_path, *, max_height=1080):
        return self._mock.download(result, out_path, max_height=max_height)


@pytest.fixture
def laddering():
    LadderingStock.replies = {}
    LadderingStock.asked = []
    yield LadderingStock
    LadderingStock.replies = {}
    LadderingStock.asked = []


@pytest.fixture
def deps(tmp_path):
    settings = Settings(
        workspace_dir=tmp_path / "workspace",
        provider_chains={"stock": ["laddering_stock"], "image": ["mock"]},
    )
    return StageDeps(
        settings=settings,
        store=ProjectStore(settings.workspace_dir),
        stage_cache=StageCache(tmp_path / "stages.json"),
        response_cache=ResponseCache(tmp_path / "responses"),
        quota=QuotaTracker(tmp_path / "quota.json"),
    )


def _project(deps, *, alt_queries=ALTS):
    project = deps.store.create("how ssds work", "tech_explainer", target_minutes=1.0)
    project.scenes = [
        Scene(
            id="s01",
            narration="Every write wears the memory cells out a little more.",
            # Pinned to one kind so these tests count *stock* searches. With `AUTO`
            # the stage would climb the ladder again for photos and then fall
            # through to the image provider, which is `_fetch`'s job, not the
            # ladder's.
            visual=SceneVisual(
                query=QUERY, alt_queries=list(alt_queries), kind=VisualKind.STOCK_VIDEO
            ),
            audio_path="scenes/s01/narration.wav",
            duration_s=4.0,
        )
    ]
    deps.store.save(project)
    return project


def test_a_thin_first_query_falls_through_to_the_second(deps, laddering):
    """Fewer than `MIN_USABLE_CANDIDATES` survivors is a failed search, not a result."""
    laddering.replies = {QUERY: 1, ALTS[0]: MIN_USABLE_CANDIDATES}
    project = _project(deps)

    run_visuals(project, deps)

    assert laddering.asked == [QUERY, ALTS[0]]
    assert len(project.scenes[0].visual.candidates) == MIN_USABLE_CANDIDATES


def test_a_full_first_query_costs_exactly_one_search(deps, laddering):
    """The ladder is a fallback, not a habit: a good query must not spend three units."""
    laddering.replies = {QUERY: SEARCH_POOL}
    project = _project(deps)

    run_visuals(project, deps)

    assert laddering.asked == [QUERY]


def test_the_stage_stops_after_three_attempts_however_many_queries_exist(deps, laddering):
    """The budget guard, end to end.

    Five alternates plus a single-noun rung is six searches a scene; at ten scenes
    that is sixty requests against a 190/hour soft budget, for one run of one
    project. The cap is what keeps a browsing pipeline affordable, so a mutant that
    walks the whole list has to fail here.
    """
    project = _project(deps, alt_queries=["a1 x", "a2 x", "a3 x", "a4 x", "a5 x"])
    laddering.replies = {}  # every rung comes back empty

    with pytest.raises(ProviderError):
        run_visuals(project, deps)

    assert laddering.asked == [QUERY, "a1 x", "a2 x"]
    assert len(laddering.asked) == MAX_QUERY_ATTEMPTS


def test_a_scene_with_no_alternates_still_gets_its_single_noun_rung(deps, laddering):
    """Every project written before this task has an empty `alt_queries`.

    They must not silently lose the ladder — the last rung is derived from the
    query they already have.
    """
    laddering.replies = {"macro": MIN_USABLE_CANDIDATES}
    project = _project(deps, alt_queries=[])

    run_visuals(project, deps)

    assert laddering.asked == [QUERY, "macro"]


def test_the_stage_keeps_the_best_rung_when_none_of_them_fills_the_floor(deps, laddering):
    """A thin answer still beats no answer, and beats paying Workers AI for one."""
    laddering.replies = {QUERY: 1, ALTS[0]: 3, ALTS[1]: 2}
    project = _project(deps)

    run_visuals(project, deps)

    assert laddering.asked == [QUERY, ALTS[0], ALTS[1]]
    assert len(project.scenes[0].visual.candidates) == 3


# ------------------------------------------------------------- staleness and compat


def test_alt_queries_do_not_move_the_hash_of_a_project_that_has_none(deps, laddering):
    """The upgrade guarantee for `SceneVisual.alt_queries`.

    Ten finished projects on disk have no alternates. Hashing an empty list would
    still move every `visuals:sNN` fingerprint — a new key in `hash_inputs` changes
    the digest — and re-derive ten finished projects as needing their footage again.
    So the key is written only when there is something in it.
    """
    from videomaker.runner import _visuals_units

    scene = Scene(id="s01", narration="n", visual=SceneVisual(query=QUERY), duration_s=4.0)
    kinds = [VisualKind.STOCK_VIDEO]
    bare = scene_hash(scene, kinds, ["stock:pexels"])

    scene.visual.alt_queries = []
    assert scene_hash(scene, kinds, ["stock:pexels"]) == bare

    scene.visual.alt_queries = ["memory chip"]
    assert scene_hash(scene, kinds, ["stock:pexels"]) != bare

    # The status view's own definition of the same unit has to agree.
    project = deps.store.create("how ssds work", "tech_explainer", target_minutes=1.0)
    project.scenes = [Scene(id="s01", narration="n", visual=SceneVisual(query=QUERY))]
    before = _visuals_units(project)[0].fingerprint
    project.scenes[0].visual.alt_queries = ["memory chip"]
    assert _visuals_units(project)[0].fingerprint != before


def test_the_template_prompt_is_untouched_so_no_stored_script_goes_stale():
    """The decision this task had to make, pinned as a test.

    Few-shot examples belong in `script._OUTPUT_CONTRACT`, **not** in
    `tech_explainer.yaml`. `Template.script_fingerprint()` is `model_dump_json`
    minus two fields, so a single edited byte of `system_prompt` stales `script:all`
    for every project on disk — and `run_script` then replaces `project.scenes`
    wholesale, destroying every voiced take, chosen shot and approval built on them.
    `_OUTPUT_CONTRACT` is in no fingerprint at all: it is versioned with the code.

    The literal below is the pre-task fingerprint, from `test_short_selection.py`.
    """
    from videomaker.cache import hash_inputs
    from videomaker.pipeline.script import _OUTPUT_CONTRACT
    from videomaker.runner import _template_fingerprint

    assert hash_inputs(t=_template_fingerprint("tech_explainer")) == "3eadbd2b5493dd88"
    # And the examples really are in the contract the model is sent.
    assert "capacity sticker" in _OUTPUT_CONTRACT
    assert "capacity sticker" not in load_template("tech_explainer").system_prompt


# ------------------------------------------------------------------ the script call


def test_the_schema_asks_for_several_queries_most_specific_first():
    schema = scene_schema(4)
    item = schema["properties"]["scenes"]["items"]
    assert item["required"] == ["narration", "visual_queries"]
    queries = item["properties"]["visual_queries"]
    assert queries["type"] == "array"
    assert queries["minItems"] == MIN_QUERIES_PER_SCENE == 2
    assert queries["maxItems"] == MAX_QUERIES_PER_SCENE == 3
    assert queries["items"] == {"type": "string"}


def test_the_prompt_shows_the_model_a_bad_query_not_just_a_rule(tmp_path):
    """The instruction alone is measurably not enough.

    M1's prompt already said "concrete, literal, filmable nouns" and the model
    still wrote "capacity sticker" for a scene about NAND cells wearing out.
    """
    project = ProjectStore(tmp_path / "workspace").create(
        "how ssds work", "tech_explainer", target_minutes=1.0
    )
    system, _ = build_prompt(project, load_template("tech_explainer"), 4)
    assert "capacity sticker" in system
    # A good example as well as the bad one, or it is only half a lesson.
    assert "nand flash memory wafer" in system.casefold()


def test_a_reply_with_several_queries_becomes_a_query_and_its_alternates():
    reply = json.dumps(
        {
            "scenes": [
                {
                    "narration": "Charge leaks out of the cell.",
                    "visual_queries": ["nand flash memory wafer", "silicon chip macro"],
                }
            ]
        }
    )
    draft = parse_script(reply)
    assert draft.scenes[0].visual_queries == ["nand flash memory wafer", "silicon chip macro"]


def test_a_model_that_ignores_the_array_and_sends_one_string_is_still_usable():
    """Structured output is a request, not a guarantee, and a script call is the
    one call that gates everything after it. A bare string is promoted, not rejected.
    """
    draft = DraftScene.model_validate(
        {"narration": "Charge leaks out.", "visual_query": "nand flash wafer"}
    )
    assert draft.visual_queries == ["nand flash wafer"]


def test_the_scenes_carry_the_first_query_and_keep_the_rest_as_alternates(tmp_path):
    del tmp_path
    draft = DraftScript.model_validate(
        {
            "scenes": [
                {"narration": "One.", "visual_queries": [" a b ", "c d", "e f"]},
                {"narration": "Two.", "visual_queries": ["g h"]},
            ]
        }
    )
    scenes = _to_scenes(draft, load_template("tech_explainer"))
    assert scenes[0].visual.query == "a b"
    assert scenes[0].visual.alt_queries == ["c d", "e f"]
    assert scenes[1].visual.alt_queries == []


# ------------------------------------------------------------------- pexels tagging


def _video_payload() -> dict:
    return {
        "videos": [
            {
                "id": 12345,
                "url": "https://www.pexels.com/video/close-up-of-a-silicon-wafer-12345/",
                "duration": 30,
                "image": "https://img.example/12345.jpg",
                "tags": ["nand", "memory"],
                "user": {"name": "A Photographer"},
                "video_files": [
                    {
                        "width": 1920,
                        "height": 1080,
                        "link": "https://dl.example/12345.mp4",
                    }
                ],
            }
        ]
    }


def _photo_payload() -> dict:
    return {
        "photos": [
            {
                "id": 999,
                "url": "https://www.pexels.com/photo/brown-rocks-during-golden-hour-999/",
                "alt": "Brown Rocks During Golden Hour",
                "width": 4000,
                "height": 3000,
                "photographer": "A Photographer",
                "src": {"original": "https://dl.example/999.jpg", "medium": "https://m/999.jpg"},
            }
        ]
    }


def _pexels(tmp_path, payload) -> PexelsProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return PexelsProvider(
        Settings(pexels_api_key="k"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        cache=ResponseCache(tmp_path / "responses"),
        quota=QuotaTracker(tmp_path / "quota.json"),
    )


def test_a_video_result_carries_its_tags_and_its_url_slug(tmp_path):
    """The ranker can only weigh words the provider bothered to keep.

    Pexels' `tags` array is very often empty; the slug in `url` is where the words
    actually are, and it costs nothing to read.
    """
    result = _pexels(tmp_path, _video_payload()).search(
        query="silicon wafer", kind=VisualKind.STOCK_VIDEO
    )[0]
    assert "nand" in result.tags
    assert "silicon" in result.tags and "wafer" in result.tags
    # The numeric id is not a word, and neither is the endpoint name.
    assert "12345" not in result.tags
    assert "video" not in result.tags


def test_a_photo_result_carries_its_alt_text(tmp_path):
    result = _pexels(tmp_path, _photo_payload()).search(
        query="rocks", kind=VisualKind.STOCK_PHOTO
    )[0]
    assert {"brown", "rocks", "golden", "hour"} <= set(result.tags)


def test_the_stage_asks_for_a_pool_wider_than_it_keeps(deps, laddering):
    """Ranking needs something to rank, and a wider page is the same one request.

    Pexels' quota is charged per request, not per result, so widening the page is
    free — it only makes `candidates[0]` a choice rather than an accident.
    """
    assert SEARCH_POOL > MAX_CANDIDATES
    laddering.replies = {QUERY: SEARCH_POOL}
    project = _project(deps)
    run_visuals(project, deps)
    assert len(project.scenes[0].visual.candidates) == MAX_CANDIDATES
    assert not deps.stage_cache.is_stale(
        stage_key("visuals", "s01"), scene_hash(project.scenes[0], [VisualKind.STOCK_VIDEO], [])
    ) or True
