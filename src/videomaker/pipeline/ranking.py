"""Choosing *which* stock result to use, and *what else* to ask for.

M1 searched once and took `candidates[0]`. That is how scene 8 of the SSD video
ended up showing a warehouse shelf stencilled "LOAD CAPACITY PER SHELF 200 KG":
the query was `"capacity sticker"`, Pexels matched the keyword perfectly, and
nothing downstream ever asked whether the shot was about NAND cells wearing out.
See `docs/visual-search-design.md`.

Everything in this module is a **pure function** over data the search response
already carried — tags, duration, pixel dimensions. No model, no quota, no new
dependency, and so no reason not to run it on every candidate of every scene.
It is deliberately separate from `visuals.py`, which owns the network, the cache
and the provider chain: the judgement is the part worth testing exhaustively, and
it is only testable exhaustively while it stays free of all three.

Four pieces, in the order the stage uses them:

* `query_ladder` — what to ask, in order, capped.
* `drop_cliches` — the answers Pexels gives when it has understood nothing.
* `rank_candidates` — the order to offer the rest in.
* `is_ambiguous` — whether that order was decided by anything, which is the only
  question that can justify spending a vision request on the scene (item 4).
"""

import re

from videomaker.models import StockResult
from videomaker.pipeline.base import SCENE_GAP_S

#: How many searches one scene may ever spend. Pexels' soft budget is 190 requests
#: an hour and a ten-scene project re-runs often, so laddering has to be bounded by
#: something other than optimism. Three rungs is already a 3x worst case.
MAX_QUERY_ATTEMPTS = 3

_WORD = re.compile(r"[a-z]+")
_MIN_WORD_LEN = 3
_PLURAL_MIN_LEN = 4

#: Words that carry no visual meaning. Short by design — this is a matcher, not a
#: linguist, and the length floor above already removes most function words.
_STOPWORDS = frozenset(
    {
        "and", "are", "but", "for", "from", "has", "have", "how", "into", "its", "like", "made",
        "make", "more", "most", "not", "now", "off", "one", "only", "onto", "other", "our", "over",
        "per", "she", "some", "such", "than", "that", "the", "their", "them", "then", "there",
        "these", "they", "this", "those", "through", "too", "under", "until", "upon", "use", "used",
        "very", "was", "were", "what", "when", "where", "which", "while", "who", "whom", "why",
        "will", "with", "within", "you", "your", "all", "also", "any", "been", "being", "both",
        "each", "does", "doing", "done", "down", "during", "even", "ever"
    }
)

#: The recognisable class of wrong answer: Pexels' fallback to business stock when
#: it has matched nothing in the query. Deliberately **small**. `_fetch` falls
#: through to Workers AI when stock comes back empty, and Workers AI bills
#: automatically past the free neuron cap (M0 finding 6) — so an over-eager
#: denylist does not merely lose a shot, it spends money. Every entry is a single
#: word in the form `keywords()` produces, or it could never match a tag.
CLICHE_TAGS = frozenset(
    {
        "handshake",
        "teamwork",
        "businessman",
        "businesswoman",
        "businesspeople",
        "coworker",
        "colleague",
        "boardroom",
        "brainstorming",
        "corporate",
        "entrepreneur",
        "smiling",
        "office",
        "necktie",
    }
)

# Ranking weights. Tag overlap with the query dominates on purpose: it is the only
# signal that says the clip is *about* the right thing. The rest break ties.
QUERY_WEIGHT = 3.0
NARRATION_WEIGHT = 1.0
HEADROOM_WEIGHT = 0.5
RESOLUTION_WEIGHT = 0.5

#: Narration is a whole sentence, so its overlap is scored by count, not fraction —
#: three matching tags is already a clip that understood the scene.
NARRATION_MATCH_CAP = 3
#: Seconds of spare footage past which more is not better; the trim only needs room.
FULL_HEADROOM_S = 5.0
#: A still cannot be too short. Neutral, so photos are neither preferred nor punished.
NEUTRAL_HEADROOM = 0.5
#: The provider already filtered below `BASELINE_SIDE`; `FULL_SIDE` is 4K.
BASELINE_SIDE = 1080
FULL_SIDE = 2160


def keywords(text: str) -> set[str]:
    """The content words of `text`, lowercased and crudely singularised.

    The singularisation is one rule — drop a trailing `s` that is not part of `ss` —
    because the two bags being intersected are a human-written query and a stock
    library's tags, and those disagree about plurals constantly. "the cells wear
    out" and a clip tagged "cell" are about the same thing; a matcher that scores
    them zero is measuring spelling.
    """
    found = set()
    for word in _WORD.findall(text.lower()):
        if len(word) < _MIN_WORD_LEN or word in _STOPWORDS:
            continue
        if len(word) >= _PLURAL_MIN_LEN and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        if word not in _STOPWORDS:
            found.add(word)
    return found


def single_noun(query: str) -> str:
    """The last content word of `query` — English compounds are head-final.

    "close up circuit board solder" is a shot of solder; "capacity sticker" is a
    sticker, not a capacity. The head is both the most concrete term and the one a
    keyword-matching stock library has the best chance of having footage of, which
    is exactly what the last rung of the ladder needs. Empty when there is no
    content word to take.
    """
    words = [
        word
        for word in _WORD.findall(query.lower())
        if len(word) >= _MIN_WORD_LEN and word not in _STOPWORDS
    ]
    return words[-1] if words else ""


def query_ladder(
    query: str,
    alt_queries: list[str],
    *,
    limit: int = MAX_QUERY_ATTEMPTS,
) -> list[str]:
    """What to search for, most specific first, deduplicated and capped at `limit`.

    The alternates come from the script call, which was asked for two or three
    orderings of the same idea at no extra cost. The single-noun rung is only added
    when there is room left: it is the crudest query in the list, so it must never
    displace one the model wrote, and a project written before `alt_queries` existed
    is exactly the case it exists for.
    """
    rungs: list[str] = []
    seen: set[str] = set()
    for candidate in [query, *alt_queries]:
        text = candidate.strip()
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            rungs.append(text)
    if len(rungs) < limit:
        noun = single_noun(query)
        if noun and noun.casefold() not in seen:
            rungs.append(noun)
    return rungs[:limit]


def is_cliche(result: StockResult, *, query: str) -> bool:
    """True for a result that matched nothing asked for and is generic business stock.

    Both halves matter. Search "open plan office desk" and an office is the correct
    answer, not a cliché — so a candidate that overlaps the query at all is never
    denylisted. It is only when a result shares **no** word with the query that its
    handshake-and-boardroom tags are all it has, and that is the case M1 kept
    shipping.
    """
    tags = keywords(" ".join(result.tags))
    if tags & keywords(query):
        return False
    return bool(tags & CLICHE_TAGS)


def drop_cliches(results: list[StockResult], *, query: str) -> list[StockResult]:
    """`results` without the business-stock clichés, order preserved."""
    return [result for result in results if not is_cliche(result, query=query)]


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _headroom_score(duration_s: float | None, min_duration_s: float) -> float:
    """How comfortably a clip covers the scene *and* the gap before the next cut."""
    if duration_s is None:
        return NEUTRAL_HEADROOM
    return _clamp((duration_s - (min_duration_s + SCENE_GAP_S)) / FULL_HEADROOM_S)


def score_candidate(
    result: StockResult,
    *,
    query: str,
    narration: str,
    min_duration_s: float = 0.0,
) -> float:
    """How well `result` suits this scene, from metadata alone. Higher is better.

    Four signals, all free, none of which can see the picture — that limit is real
    and is why item 4 of the design doc exists. What this does buy is that
    `candidates[0]` stops being "whatever the API listed first".
    """
    tags = keywords(" ".join(result.tags))
    wanted = keywords(query)
    # Minus the query's own words, so the narration is *independent* evidence rather
    # than the same match counted twice.
    spoken = keywords(narration) - wanted

    query_score = len(tags & wanted) / len(wanted) if wanted else 0.0
    narration_score = min(len(tags & spoken), NARRATION_MATCH_CAP) / NARRATION_MATCH_CAP
    headroom = _headroom_score(result.duration_s, min_duration_s)
    short_side = min(result.width, result.height)
    resolution = _clamp((short_side - BASELINE_SIDE) / (FULL_SIDE - BASELINE_SIDE))

    return (
        QUERY_WEIGHT * query_score
        + NARRATION_WEIGHT * narration_score
        + HEADROOM_WEIGHT * headroom
        + RESOLUTION_WEIGHT * resolution
    )


def rank_candidates(
    results: list[StockResult],
    *,
    query: str,
    narration: str,
    min_duration_s: float = 0.0,
) -> list[StockResult]:
    """`results` best first. Ties keep the provider's own order — `sorted` is stable.

    That stability is a decision, not an accident: Pexels has already sorted by its
    own relevance, and where this function has nothing to say, "leave it alone" is
    strictly better than a reshuffle nobody can explain.
    """
    return sorted(
        results,
        key=lambda result: score_candidate(
            result, query=query, narration=narration, min_duration_s=min_duration_s
        ),
        reverse=True,
    )


# ------------------------------------------------------------------- the re-rank gate

#: Below this the metadata leader has not matched **half** the words the scene asked
#: for. `QUERY_WEIGHT / 2` is that statement written in the ranker's own units, so it
#: moves if the weights ever do rather than becoming a stale magic number.
RERANK_SCORE_FLOOR = QUERY_WEIGHT / 2

#: A lead smaller than one matching narration keyword — `NARRATION_WEIGHT /
#: NARRATION_MATCH_CAP`, a third of a point — is not evidence. It is the resolution
#: and headroom tie-breakers deciding, and neither of those says anything about what
#: the clip *shows*. 0.35 is that one-tag step with a hair of slack for float noise;
#: it is deliberately below two tags, because a two-tag lead is a real opinion.
RERANK_MARGIN = 0.35


def scores_for(
    results: list[StockResult],
    *,
    query: str,
    narration: str,
    min_duration_s: float = 0.0,
) -> list[float]:
    """`score_candidate` over a whole field, in the field's own order."""
    return [
        score_candidate(result, query=query, narration=narration, min_duration_s=min_duration_s)
        for result in results
    ]


def is_ambiguous(scores: list[float]) -> bool:
    """True when metadata alone has not decided this scene — the only time vision is
    worth a request.

    Gemini's soft budget is 240 requests a day and the script stage's LLM fallback
    spends from the same one, so "re-rank everything" is not a free default: ten
    scenes a run would take a twenty-fourth of the day per video, for scenes where
    the answer was already clear. Two conditions open the gate:

    * **Low** — the leader is under `RERANK_SCORE_FLOOR`, so nothing in the field
      matched much of what was asked for.
    * **Ambiguous** — the top two are within `RERANK_MARGIN`, so the order came from
      tie-breakers rather than from evidence about content.

    What it deliberately does **not** catch is a confident wrong answer: M1's
    warehouse shelf stencilled "LOAD CAPACITY PER SHELF 200 KG" scores *perfectly*
    against the query "capacity sticker", and no gate reading those same metadata can
    know better. Fixing that class of failure is item 1 of the design doc — a query
    worth matching — not this one.
    """
    if len(scores) < 2:
        return False
    ranked = sorted(scores, reverse=True)
    return ranked[0] < RERANK_SCORE_FLOOR or (ranked[0] - ranked[1]) < RERANK_MARGIN
