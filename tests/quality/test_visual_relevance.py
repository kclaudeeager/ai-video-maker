"""The before/after number for stock relevance — `docs/visual-search-design.md`.

*"The task is not done without a before/after number."* That is the design doc's
binding condition on the whole visual-search workstream, and this module is the
only thing in the repository that can satisfy it. Everything else about Tasks 12
and 13 is unit-tested behaviour: the ladder climbs, the ranker orders, the gate
gates. None of that says the pictures got better.

**This is not a unit test and does not behave like one.** It is marked `slow`, it
talks to Pexels, Groq and (arm 3 only) Gemini, and it deliberately shares the real
`~/.cache/ai-video-maker` response cache and quota ledger that `tests/conftest.py`
redirects away from every other test — because the thing being measured *is* live
provider behaviour, and because a shared cache is what makes a second run free.
Nothing here runs in CI, and nothing here runs on a plain `uv run pytest -q` even
on a machine that has every key: it is opt-in behind `RELEVANCE_HARNESS=1`.

    RELEVANCE_HARNESS=1 uv run pytest tests/quality -q -s

## The four arms

| arm | queries written by | clip chosen by |
|---|---|---|
| `before` | M1's prompt (one query/scene) | M1's path: `search()[0]` |
| `before_same_query` | Task 12's prompt | M1's path: `search()[0]` |
| `metadata` | Task 12's prompt | Task 12: ladder + cliché drop + rank |
| `vision` | Task 12's prompt | Task 12 + Task 13's Gemini re-rank |

`before` is not reconstructed — it is read off the ten finished projects in the
owner's own workspace, whose shots were picked by the real M1 code before any of
this existed. It is the honest historical baseline and it costs nothing.

`before_same_query` is the control that `before` cannot be. `before` differs from
`metadata` in *two* things at once — the prompt and the selection logic — and its
narration is different text entirely, so a paired scene-by-scene comparison is not
available. `before_same_query` holds the narration and the query fixed and changes
only the selection logic, which splits the improvement into the half the prompt
bought and the half the ranker bought.

It costs nothing either. `MAX_CANDIDATES`-wide and `SEARCH_POOL`-wide searches for
the same query are the same page of Pexels results in the same order — verified
directly, three queries, the leading ids identical and prefix-matching — so M1's
`search(per_page=MAX_CANDIDATES)[0]` is exactly the first survivor of the pool the
ladder's first rung already fetched. Reading it back out spends nothing new.

## What is faithfully reproduced, and what is not

Reproduced: the real script stage with the shipped prompt, the real Pexels
provider, the real `_search_ladder`, `drop_cliches`, `rank_candidates` and
`_rerank`, through the real `run_visuals` on a real `StageDeps`.

Not reproduced, both deliberately:

* **The download.** `PexelsProvider.download` is stubbed. The harness measures
  *which* clip is chosen; fetching two hundred 1080p clips would measure the
  network.
* **The voice stage.** `visuals` runs after `voice` in `STAGE_ORDER`, so in
  production `Scene.duration_s` is set and Pexels filters clips shorter than the
  scene. Synthesising 100 scenes of Kokoro to recover one float per scene is not
  worth it, so `duration_s` is estimated from the word count at `SPEAKING_RATE_WPS`
  — which is not a guess: it is measured from the 2,400-odd words and 101 real
  narration recordings of the very projects arm `before` reads.

The image provider is unconfigured on purpose. `tech_explainer`'s
`visual_kind_order` ends in `ai_image`, and Workers AI bills automatically past its
free cap (M0 finding 6); a harness must not be able to spend money to paper over a
scene stock could not serve. A scene with no stock result is left without a chosen
visual and counts as a miss, which is what it is.

## Scoring

An LLM judge on Groq, `temperature=0`, one pinned model, ten pairs to a request,
cached — the design doc's own second option. Hand-rating ~300 distinct pairs once
is not re-runnable, and a number nobody can reproduce next month is not a baseline.

The rubric is **calibrated against the one hand rating that exists**: M1 rated the
ten `how-ssds-work` shots 6 of 10 strongly on topic, and the judge is tuned until it
reproduces that split. See `JUDGE_SYSTEM` for what the first draft got wrong and why
calibrating on the *baseline* arm cannot flatter the others.

Two decisions make the judge worth believing:

* **It is blind.** Pairs are deduplicated across arms before judging and batched in
  hash order, so the judge is never told which arm a clip came from and never sees
  one scene's competing clips side by side. Grouped, it would be ranking the arms
  against each other; scattered, it answers the question the pipeline needs answered,
  which is whether one clip suits one scene.
* **It sees the same field for every arm.** The clip description is the Pexels
  slug — the library's own human-readable title, `"a-person-typing-on-the-keyboard"`
  — and nothing else. `StockResult.tags` is richer, but it exists only for arms that
  ran today; `AssetRef` never carried it, so the finished projects do not have it.
  Feeding the newer arms a fatter description than the baseline would manufacture
  part of the improvement.

The circularity risk is real and is written down rather than waved away: a text
judge reads roughly the signal `ranking.score_candidate` optimises, so it could in
principle flatter the ranked arms. Two checks answer it, both in
`docs/visual-relevance-baseline.md`. The rubric is calibrated on the *unranked*
baseline until it reproduces a human's rating of it; and 24 picks were then rated by
eye from the actual thumbnails and extracted frames, blind to the judge, agreeing
20 of 24 on the strong/not split with every disagreement in the *strict* direction.
An unvalidated judge is a number with no meaning.

**The result, for the reader who came here from the code rather than the doc: the
target was 8/10, the shipped default measures 3.4/10, and no arm is distinguishable
from any other.** `docs/visual-relevance-baseline.md` has the failure taxonomy.
"""

import json
import os
import re
import shutil
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from functools import partial
from pathlib import Path
from urllib.parse import urlparse

import pytest

from videomaker.cache import ResponseCache, StageCache, hash_inputs
from videomaker.config import Settings, load_settings
from videomaker.models import AssetRef, Project, Scene, StockResult, VisualKind
from videomaker.pipeline.base import StageDeps
from videomaker.pipeline.script import run_script
from videomaker.pipeline.visuals import (
    MAX_CANDIDATES,
    ORIENTATION,
    SEARCH_POOL,
    resolve_kinds,
    run_visuals,
)
from videomaker.project import ProjectStore
from videomaker.providers import get_provider
from videomaker.providers.base import LLMProvider, StockProvider
from videomaker.providers.errors import (
    ProviderError,
    ProviderResponseError,
    QuotaExceeded,
    TransientError,
)
from videomaker.providers.ratelimit import SOFT_BUDGETS, QuotaTracker
from videomaker.providers.stock.pexels import PexelsProvider
from videomaker.templates import load_template

pytestmark = pytest.mark.slow

# --------------------------------------------------------------------- the constants

#: M1's definition-of-done verification, `docs/superpowers/spike-results-m1.md`
#: finding 9: "Six of ten scenes are strongly on topic". Rated by hand, on the ten
#: scenes of `how-ssds-work`, before any of this existed.
M1_BASELINE_SHARE = 0.6
#: `docs/visual-search-design.md`: "8/10 strongly on topic is a reasonable bar".
TARGET_SHARE = 0.8
#: How much of M1's hand rating the judge has to still reproduce to be believed.
#: The judge as calibrated reproduces it exactly — 10 of 10 scenes, and the same
#: 6/10 total — so this has real headroom rather than being fitted to the result.
JUDGE_AGREEMENT_FLOOR = 0.8

#: What this harness measured on 2026-08-31 for the shipped default, over all ten
#: projects. **It is not an acceptable number and it is not the target** — see
#: `docs/visual-relevance-baseline.md`, where the miss is written down in full. It
#: is here so that a future change which makes visual search *worse* fails a test
#: instead of going unnoticed, which is the only guarantee a 3.4/10 can offer.
MEASURED_SHIPPED_SHARE = 0.34
#: One standard error on 100 scenes at this rate is about 0.05, so anything inside
#: that is the same measurement rather than a regression.
REGRESSION_TOLERANCE = 0.05

#: The ten topics, which are the owner's ten real projects. Held here rather than
#: read off the workspace so the after-arms are reproducible on a machine that has
#: never made a video; the `before` arm still needs the finished projects and skips
#: itself without them.
#: `(project id, topic, target minutes)`.
type Topics = Sequence[tuple[str, str, float]]

TOPICS: tuple[tuple[str, str, float], ...] = (
    ("a-spreadsheet-is-not-paper", "a spreadsheet is not paper", 5.0),
    (
        "docx-pdf-and-csv-and-why-the-difference-matters",
        "docx pdf and csv and why the difference matters",
        5.0,
    ),
    ("how-ssds-work", "how ssds work", 2.0),
    ("how-to-sync-your-apple-devices", "how to sync your apple devices", 2.0),
    ("never-type-the-same-thing-twice", "never type the same thing twice", 5.0),
    ("save-your-file-and-then-find-it-again", "save your file and then find it again", 5.0),
    (
        "what-the-cloud-actually-means-for-your-documents",
        "what the cloud actually means for your documents",
        5.0,
    ),
    (
        "why-copy-and-paste-breaks-your-formatting",
        "why copy and paste breaks your formatting",
        5.0,
    ),
    (
        "why-your-document-falls-apart-every-time-you-edit-it",
        "why your document falls apart every time you edit it",
        5.0,
    ),
    (
        "why-your-slides-are-putting-people-to-sleep",
        "why your slides are putting people to sleep",
        5.0,
    ),
)

TEMPLATE = "tech_explainer"

#: Words per second of finished narration, measured over all 101 scenes of the ten
#: real projects: 2,486 words against 1,010.4 s of Kokoro audio. Stands in for the
#: voice stage so the Pexels duration filter behaves as it does in production.
SPEAKING_RATE_WPS = 2.461

#: Deliberately the developer's real cache root, which `tests/conftest.py` hides
#: from every other test. See the module docstring: the point of this harness is
#: live provider behaviour, and sharing the pipeline's own cached search pages is
#: what keeps a re-run at zero quota.
USER_CACHE_ROOT = Path.home() / ".cache" / "ai-video-maker"

#: The opt-in switch. See the `keys` fixture: `slow` does not stop a plain
#: `uv run pytest -q` on a machine that has the keys, and this one spends money's
#: worth of somebody's free tier.
OPT_IN_ENV = "RELEVANCE_HARNESS"

#: Where the raw per-scene verdicts land, for `docs/visual-relevance-baseline.md` to
#: be written from. Under `workspace/`, which is gitignored.
DEFAULT_OUT = Path("workspace/quality/visual-relevance.json")

ARMS = ("before", "before_same_query", "metadata", "vision")

_SLUG_ID = re.compile(r"-\d+$")
_SLUG_WORD = re.compile(r"[a-z]+")


# ------------------------------------------------------------------ pacing for Groq

#: Groq's on-demand tier limits `openai/gpt-oss-120b` to 8,000 tokens a minute and
#: counts the *reserved* `max_tokens`, so both the ten script calls and the few
#: hundred judge calls will be rate limited however politely they are paced.
#: `HttpLLMProvider.generate` maps a 429 onto `TransientError` and deliberately does
#: not retry — a pipeline stage would rather fail over to the next provider than
#: sit and wait. A harness has nowhere to fail over to, so it waits.
MAX_ATTEMPTS = 8
BACKOFF_S = 15.0

#: The project's *own* soft budget bites before Groq's does: `SOFT_BUDGETS["groq"]`
#: is 28 requests a minute and `QuotaTracker.check` raises rather than waits. A few
#: under 28 leaves room for the script calls sharing the same ledger.
LIVE_CALL_SPACING_S = 60.0 / 24


def with_retry[T](call: Callable[[], T], *, what: str) -> T:
    """Run `call`, waiting out both rate limits it can meet.

    `TransientError` is Groq saying so, and carries its own `Retry-After`.
    `QuotaExceeded` is *this project* saying so, from `QuotaTracker`; Groq's budget
    is `rpm`-only, so the window it names always drains — a provider with a daily
    cap would need this to give up rather than sleep through the night.
    """
    for attempt in range(MAX_ATTEMPTS):
        try:
            return call()
        except QuotaExceeded:
            if attempt == MAX_ATTEMPTS - 1:
                raise
            print(f"  {what}: local rpm budget spent, waiting {BACKOFF_S:.0f}s")
            time.sleep(BACKOFF_S)
        except TransientError as exc:
            if attempt == MAX_ATTEMPTS - 1:
                raise
            wait = exc.retry_after_s or BACKOFF_S
            print(f"  {what}: rate limited, waiting {wait:.0f}s")
            time.sleep(wait + 1.0)
    raise AssertionError("unreachable")


# ------------------------------------------------------------------------ the judge

#: **Calibrated against M1's own hand rating, not written from taste.**
#:
#: The first draft of this rubric asked whether the clip illustrated the sentence,
#: and rated the ten `how-ssds-work` shots M1 rated by hand at **1 of 10** where the
#: human had said 6 — a ruler pinned at zero, with no resolution to measure an
#: improvement with. Explainer B-roll almost never illustrates the sentence; what
#: the human was actually accepting was *a picture of the real thing the video is
#: about*, and what they were rejecting was "generic tech B-roll".
#:
#: So the bar was rewritten to say that, and only that, and then checked against the
#: same ten shots. It now scores them **5 of 10** against the human's 6, and agrees
#: with the human on **9 of 10** individual scenes — the one disagreement being the
#: cooling fans of scene 10, which the human allowed and this does not. The two
#: named exclusions (CGI animation, server rooms for scenes not about servers) are
#: the classes the human called generic; they are in the rubric because the human
#: applied them, not because they made a number come out.
#:
#: Calibrating on the **baseline** arm is deliberate. A ruler tuned on the after-arms
#: could flatter them; one tuned to reproduce a hand rating of the *before* picture
#: cannot.
JUDGE_SYSTEM = (
    "You rate stock footage for one scene of a narrated explainer video. You are "
    "given the sentence the narrator speaks over the shot, and the stock library's "
    "own one-line description of the clip that was chosen for it.\n\n"
    "The question is whether a viewer would accept this shot as belonging to this "
    "scene \u2014 not whether it depicts the sentence literally. Explainer B-roll almost "
    "never depicts the sentence literally, and a shot that plainly shows the real "
    "thing the scene is about is doing its job.\n\n"
    "2 = strongly on topic. The clip shows a real, specific thing this scene is "
    "about, or the hardware, software, document or human activity that thing lives "
    "in \u2014 even when the narration describes a property of it you could never see in "
    "a shot. A spinning hard drive over a scene about disk latency is a 2. Someone "
    "typing on a keyboard over a scene about saving a file is a 2. Fans inside a "
    "computer case over a scene about a drive inside that computer is a 2. A "
    "close-up of a real circuit board over a scene about the chips on it is a 2. Do "
    "not require the clip to illustrate the specific claim.\n\n"
    "1 = generic filler. Its subject is not a real thing this scene is about, only "
    "something inoffensive that would sit equally well under any video: a stock "
    "office, a meeting, an anonymous person in front of an unrelated screen, code on "
    "a screen for a scene that is not about code. Two particular kinds of filler "
    "always belong here however well their words match \u2014 abstract, glowing or "
    '"futuristic" computer-generated animation, which is a picture of nothing; and '
    "server rooms or data centres used for a scene that is not about servers.\n\n"
    "0 = wrong. The subject belongs to a different world entirely \u2014 pharmaceuticals, "
    "farming, sport, medicine, construction for a scene about computers \u2014 or it is a "
    "literal match on a word that means something else here, such as a warehouse "
    "shelf labelled with a weight limit for a scene about memory capacity.\n\n"
    "Judge the subject only. You cannot see the picture and must not guess at "
    "photography, lighting or quality. Reply with JSON only."
)

#: The ten `how-ssds-work` scenes M1 rated by hand, and how it rated them:
#: "Six of ten scenes are strongly on topic (keyboard, HDD platter x2, PCB macro x2,
#: PC interior); four are generic tech B-roll". Kept here so a future edit to the
#: rubric can be re-checked against the same anchor rather than against taste.
M1_HAND_RATING: dict[str, bool] = {
    "s01": True, "s02": True, "s03": True, "s04": False, "s05": True,
    "s06": False, "s07": False, "s08": False, "s09": True, "s10": True,
}
M1_ANCHOR_PROJECT = "how-ssds-work"

JUDGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdicts"],
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["n", "score", "why"],
                "properties": {
                    "n": {"type": "integer"},
                    "score": {"type": "integer"},
                    "why": {"type": "string"},
                },
            },
        }
    },
}

#: A ruler that moves between two runs is not a ruler. The model is pinned for the
#: same reason the chain is: `GROQ_MODEL_PREFERENCE` leads with `gpt-oss-120b`,
#: whose 200,000-token daily allowance one unbatched pass over 300 pairs exhausts
#: outright, and a judge that silently changed model mid-measurement would be
#: measuring two different things and reporting one number.
JUDGE_MODEL_PREFERENCE = ("qwen/qwen3.8-27b", "openai/gpt-oss-120b", "openai/gpt-oss-20b")
JUDGE_TEMPERATURE = 0.0

#: Pairs per request. Groq counts the *reserved* `max_tokens` against both the
#: per-minute and the per-day token allowance, so one call per pair spends ~1,100
#: tokens to return thirty — 300 pairs is more than a day's budget for the model.
#: Ten to a call amortises the 450-token rubric across ten verdicts and brings the
#: whole measurement inside a few percent of the allowance.
JUDGE_BATCH = 10
#: Enough for `JUDGE_BATCH` verdicts plus the reasoning a `gpt-oss` model spends
#: before writing any of them. Measured, both ends: 256 truncates a *single* verdict
#: and 1600 truncates a batch of ten, and Groq rejects a truncated completion as
#: `json_validate_failed` rather than returning the fragment.
JUDGE_MAX_TOKENS = 3000

def judge_prompt(pairs: Sequence[tuple[str, str]]) -> str:
    """The batch, numbered. Items are unrelated to each other and say so."""
    items = "\n\n".join(
        f"[{index}]\nNarration: {narration}\nClip description: {description}"
        for index, (narration, description) in enumerate(pairs)
    )
    return (
        f"Rate each of the following {len(pairs)} scene-and-clip pairs. They come "
        "from different videos and have nothing to do with each other; judge each "
        "one entirely on its own.\n\n"
        f"{items}\n\n"
        'Reply with {"verdicts": [{"n": <index>, "score": 0|1|2, "why": '
        '"<one short sentence>"}, ...]} '
        f"— exactly {len(pairs)} entries, one per index, in order."
    )


def judge_batch(
    llm: LLMProvider, pairs: Sequence[tuple[str, str]]
) -> tuple[list[tuple[int, str]], int]:
    """Verdicts for one batch, and how many live calls it took.

    Caching is the provider's own `ResponseCache`, keyed on the exact prompt: an
    edited rubric is a different prompt and cannot be answered from an old verdict,
    and an unedited one re-runs for free.

    **A batch that will not fit is split, not discarded.** Groq refuses a truncated
    completion outright — `400 json_validate_failed`, no fragment returned — and how
    much room ten verdicts need depends on how long that batch's narrations are. So
    a refusal halves the batch and tries again, down to a single pair; only a pair
    that fails alone scores 0, and it says why. Zeroing a whole batch because one
    scene's narration ran long would put ten fictitious failures into the number.
    """
    try:
        result = with_retry(
            lambda: llm.generate(
                system=JUDGE_SYSTEM,
                user=judge_prompt(pairs),
                json_schema=JUDGE_SCHEMA,
                temperature=JUDGE_TEMPERATURE,
                max_tokens=JUDGE_MAX_TOKENS,
            ),
            what="judge",
        )
    except ProviderResponseError as exc:
        if len(pairs) == 1:
            return [(0, f"judge refused this pair: {exc}")], 1
        middle = len(pairs) // 2
        left, left_calls = judge_batch(llm, pairs[:middle])
        right, right_calls = judge_batch(llm, pairs[middle:])
        return left + right, left_calls + right_calls

    live = 0 if result.cached else 1
    answered: dict[int, tuple[int, str]] = {}
    try:
        entries = json.loads(result.text)["verdicts"]
    except (ValueError, KeyError, TypeError):
        entries = []
    for entry in entries:
        try:
            index, score = int(entry["n"]), int(entry["score"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= index < len(pairs) and 0 <= score <= 2:
            answered[index] = (score, str(entry.get("why", "")))

    if len(answered) < len(pairs):
        # A dropped index is the model losing count, not a judgement. Ask again in
        # smaller groups rather than booking a 0 nobody arrived at — a silent 0 is
        # indistinguishable from a genuinely wrong clip and would inflate every
        # failure count in the report.
        if len(pairs) == 1:
            return [(0, f"judge returned no verdict: {result.text[:120]!r}")], live
        middle = len(pairs) // 2
        left, left_calls = judge_batch(llm, pairs[:middle])
        right, right_calls = judge_batch(llm, pairs[middle:])
        return left + right, live + left_calls + right_calls
    return [answered[index] for index in range(len(pairs))], live


# ------------------------------------------------------------------------- the picks


@dataclass(frozen=True)
class Pick:
    """One scene's chosen shot, in whichever arm chose it."""

    arm: str
    project: str
    scene: str
    narration: str
    query: str
    source_id: str
    source_url: str
    preview_url: str
    score: int = -1
    why: str = ""


def clip_description(source_url: str) -> str:
    """The library's own words for a clip: its slug, minus the trailing id.

    The one description every arm has. `AssetRef` never carried `tags`, so the ten
    finished projects cannot offer more than this, and giving the newer arms more
    would be measuring the harness rather than the pipeline.
    """
    segments = [part for part in urlparse(source_url).path.split("/") if part]
    if not segments:
        return ""
    words = _SLUG_WORD.findall(_SLUG_ID.sub("", segments[-1]).lower())
    # "video"/"photo" is the URL's own path noise, not a word about the subject.
    return " ".join(word for word in words if word not in {"video", "photo"})


def estimated_duration_s(narration: str) -> float:
    """What the voice stage would have measured, near enough for a search filter."""
    return len(narration.split()) / SPEAKING_RATE_WPS


def _pick(arm: str, project_id: str, scene: Scene, ref: AssetRef | StockResult | None) -> Pick:
    return Pick(
        arm=arm,
        project=project_id,
        scene=scene.id,
        narration=scene.narration,
        query=scene.visual.query,
        source_id=getattr(ref, "source_id", ""),
        source_url=getattr(ref, "source_url", ""),
        preview_url=getattr(ref, "preview_url", ""),
    )


# ------------------------------------------------------------------- arm 1: the past


def read_before_arm(workspace: Path, topics: Topics = TOPICS) -> list[Pick]:
    """The shots the ten finished projects actually shipped with. Read only.

    Nothing here writes to the owner's workspace, and nothing here re-runs a stage
    against it: `ProjectStore.load` and the JSON on disk are the whole arm.
    """
    store = ProjectStore(workspace)
    picks: list[Pick] = []
    for project_id, _topic, _minutes in topics:
        try:
            project = store.load(project_id)
        except (FileNotFoundError, ValueError):
            continue
        picks.extend(_pick("before", project_id, scene, scene.visual.chosen)
                     for scene in project.scenes)
    return picks


# --------------------------------------------------------- arms 2-4: today's pipeline


def _stub_download(
    _self: PexelsProvider, result: StockResult, out_path: Path, *, max_height: int = 1080
) -> AssetRef:
    """`download` without the bytes. Which clip was chosen is unaffected by fetching it."""
    del max_height
    return AssetRef(
        provider=result.provider,
        source_id=result.source_id,
        source_url=result.source_url,
        local_path=out_path.name,
        preview_url=result.preview_url,
        width=result.width,
        height=result.height,
        duration_s=result.duration_s,
        attribution=result.attribution,
        license=result.license,
    )


def harness_settings(workspace: Path, *, rerank: bool) -> Settings:
    """The shipped settings, pointed at a scratch workspace, with no image provider."""
    base = load_settings()
    chains = {kind: names for kind, names in base.provider_chains.items() if kind != "image"}
    return base.model_copy(
        update={
            "workspace_dir": workspace,
            "visual_rerank_enabled": rerank,
            "provider_chains": chains,
        }
    )


def build_deps(settings: Settings, project_id: str) -> StageDeps:
    """`runner.build_deps`, but on the real user cache rather than the patched one."""
    store = ProjectStore(settings.workspace_dir)
    return StageDeps(
        settings=settings,
        store=store,
        stage_cache=StageCache(store.path_for(project_id) / "cache" / "stages.json"),
        response_cache=ResponseCache(USER_CACHE_ROOT / "responses"),
        quota=QuotaTracker(USER_CACHE_ROOT / "quota.json"),
    )


def write_scripts(workspace: Path, topics: Topics = TOPICS) -> list[Project]:
    """Run the real script stage once for each topic. One Groq call per topic.

    Written once and copied into both after-arms, so `metadata` and `vision` score
    the *same* narration and the *same* queries and their difference is the re-rank
    alone. `duration_s` is filled in here — see `SPEAKING_RATE_WPS`.
    """
    store = ProjectStore(workspace)
    projects: list[Project] = []
    for project_id, topic, minutes in topics:
        settings = harness_settings(workspace, rerank=False)
        project = store.create(topic, TEMPLATE, target_minutes=minutes)
        assert project.id == project_id, f"scratch id {project.id} != {project_id}"
        deps = build_deps(settings, project.id)
        with_retry(
            partial(run_script, project, deps), what=f"script {project.id}"
        )
        for scene in project.scenes:
            scene.duration_s = estimated_duration_s(scene.narration)
        store.save(project)
        projects.append(project)
    return projects


def old_path_pick(stock: StockProvider, scene: Scene, kind: VisualKind) -> StockResult | None:
    """What M1 would have chosen for this scene: the first result, unranked.

    M1 asked for `per_page=MAX_CANDIDATES` and took `[0]`. This asks for
    `SEARCH_POOL` — the same page, the same order, and the page the ladder's first
    rung fetches anyway, so it is a cache hit and costs nothing. `MAX_CANDIDATES` is
    referenced rather than inlined because the equivalence is between those two
    constants and would stop holding if either moved.
    """
    assert SEARCH_POOL >= MAX_CANDIDATES
    results = stock.search(
        query=scene.visual.query,
        kind=kind,
        min_duration_s=scene.duration_s or 0.0,
        orientation=ORIENTATION,
        per_page=SEARCH_POOL,
    )
    return results[0] if results else None


def run_after_arm(
    source: Path, workspace: Path, *, arm: str, rerank: bool, topics: Topics = TOPICS
) -> list[Pick]:
    """Copy the scripted projects into `workspace` and run the real visuals stage."""
    shutil.copytree(source / "projects", workspace / "projects", dirs_exist_ok=True)
    settings = harness_settings(workspace, rerank=rerank)
    store = ProjectStore(workspace)
    picks: list[Pick] = []
    for project_id, _topic, _minutes in topics:
        project = store.load(project_id)
        deps = build_deps(settings, project_id)
        try:
            run_visuals(project, deps)
        except ProviderError:
            # A scene stock could not serve keeps no `chosen`, and is scored as the
            # miss it is. With no image provider configured there is nothing else it
            # could become that would not cost money.
            pass
        picks.extend(_pick(arm, project_id, scene, scene.visual.chosen)
                     for scene in project.scenes)
    return picks


def run_control_arm(scripted: Path, workspace: Path, topics: Topics = TOPICS) -> list[Pick]:
    """Arm 2: today's queries, M1's selection. Free — every search is a cache hit."""
    settings = harness_settings(workspace, rerank=False)
    store = ProjectStore(scripted)
    kind_order = load_template(TEMPLATE).visual_kind_order
    picks: list[Pick] = []
    for project_id, _topic, _minutes in topics:
        project = store.load(project_id)
        deps = build_deps(settings, project_id)
        stock = deps.provider("stock")
        assert isinstance(stock, StockProvider)
        for scene in project.scenes:
            kind = resolve_kinds(scene, kind_order)[0]
            picks.append(_pick("before_same_query", project_id, scene,
                               old_path_pick(stock, scene, kind)))
    return picks


def build_judge(settings: Settings) -> LLMProvider:
    """The judge: Groq, pinned model, sharing the pipeline's cache and ledger.

    Pinned to Groq rather than walked down the provider chain, because a fallback to
    Gemini would change the ruler halfway through the measurement — and would spend
    the same daily budget arm 3 is being judged on.
    """
    llm = get_provider("llm", "groq", settings)
    assert isinstance(llm, LLMProvider)
    llm.preference = JUDGE_MODEL_PREFERENCE  # type: ignore[attr-defined]
    llm.cache = ResponseCache(USER_CACHE_ROOT / "responses")  # type: ignore[attr-defined]
    llm.quota = QuotaTracker(USER_CACHE_ROOT / "quota.json")  # type: ignore[attr-defined]
    return llm


# ------------------------------------------------------------------------ the summary


def score_all(picks: list[Pick], llm: LLMProvider) -> tuple[list[Pick], int]:
    """Judge every distinct `(narration, description)` pair once, blind to the arm.

    Two things keep the judge honest, and both are about what it is *not* shown.
    Pairs are deduplicated across arms, so a clip three arms agreed on gets one
    verdict rather than three votes. And batches are assembled in hash order rather
    than in scene order, which scatters an arm's picks — and, more importantly,
    keeps a single scene's three or four competing clips out of the same request.
    Grouped, the model would be silently ranking the arms against each other; hashed,
    it is doing what the shipped pipeline needs judged, which is whether one clip
    suits one scene.

    Returns the scored picks and how many batches cost a live call, which is the only
    honest way to report what the measurement spent.
    """
    pairs = sorted(
        {(pick.narration, clip_description(pick.source_url)) for pick in picks if pick.source_url},
        key=lambda pair: hash_inputs(narration=pair[0], description=pair[1]),
    )
    verdicts: dict[tuple[str, str], tuple[int, str]] = {}
    live = 0
    for start in range(0, len(pairs), JUDGE_BATCH):
        batch = pairs[start : start + JUDGE_BATCH]
        answers, calls = judge_batch(llm, batch)
        verdicts.update(zip(batch, answers, strict=True))
        live += calls
        if calls:
            time.sleep(LIVE_CALL_SPACING_S)

    scored: list[Pick] = []
    for pick in picks:
        description = clip_description(pick.source_url)
        score, why = verdicts.get((pick.narration, description), (0, "no clip chosen"))
        scored.append(Pick(**{**asdict(pick), "score": score, "why": why}))
    return scored, live


def share_strongly_on_topic(picks: list[Pick]) -> float:
    """The metric: the share of scenes the judge rated 2. M1 rated this 0.6 by hand."""
    if not picks:
        return 0.0
    return sum(1 for pick in picks if pick.score == 2) / len(picks)


def summarise(scored: list[Pick]) -> dict[str, dict[str, object]]:
    summary: dict[str, dict[str, object]] = {}
    for arm in ARMS:
        arm_picks = [pick for pick in scored if pick.arm == arm]
        if not arm_picks:
            continue
        summary[arm] = {
            "scenes": len(arm_picks),
            "strong": sum(1 for pick in arm_picks if pick.score == 2),
            "generic": sum(1 for pick in arm_picks if pick.score == 1),
            "wrong": sum(1 for pick in arm_picks if pick.score == 0),
            "share": share_strongly_on_topic(arm_picks),
        }
    return summary


# ------------------------------------------------------------------------- the test


@pytest.fixture
def keys() -> Settings:
    """The shipped settings, or a skip — and the opt-in switch that guards the rest.

    `slow` alone is not enough of a guard here. CI has no keys and would skip
    anyway, but the machine this actually matters on is the owner's, where `uv run
    pytest -q` has every key in `.env` and a plain suite run would quietly spend
    an hour of Pexels budget and a chunk of the day's Gemini. So it is opt-in:

        RELEVANCE_HARNESS=1 uv run pytest tests/quality -q -s

    Nothing else in the repository needs an environment variable to run, and that
    is the point — this is the one test that costs the user something.
    """
    if not os.environ.get(OPT_IN_ENV):
        pytest.skip(f"set {OPT_IN_ENV}=1 to spend real Pexels/Groq/Gemini quota")
    settings = load_settings()
    if not (settings.pexels_api_key and settings.groq_api_key):
        pytest.skip("needs live PEXELS_API_KEY and GROQ_API_KEY")
    return settings


def test_visual_relevance_before_and_after(
    keys: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measure the four arms, write the raw verdicts, and hold the shipped default.

    The assertion is a **regression floor, not the target**. The target (8/10) is
    reported and lives in `docs/visual-relevance-baseline.md`; asserting it here
    would turn an honest miss into a permanently red suite, which is the opposite of
    what the plan asked for. What must never happen is the shipped default sliding
    back under the number M1 measured by hand, and that is what fails here.
    """
    monkeypatch.setattr(PexelsProvider, "download", _stub_download)
    scripted = tmp_path / "scripted"
    write_scripts(scripted)

    picks = read_before_arm(keys.workspace_dir)
    picks += run_control_arm(scripted, tmp_path / "control")
    picks += run_after_arm(scripted, tmp_path / "metadata", arm="metadata", rerank=False)
    picks += run_after_arm(scripted, tmp_path / "vision", arm="vision", rerank=True)

    scored, live_calls = score_all(picks, build_judge(keys))

    summary = summarise(scored)
    anchor = [p for p in scored if p.arm == "before" and p.project == M1_ANCHOR_PROJECT]
    agreed = sum(1 for p in anchor if (p.score == 2) is M1_HAND_RATING.get(p.scene, False))
    print(
        f"{'judge vs M1 by hand':20s} agrees on {agreed}/{len(anchor)} of the scenes "
        f"M1 rated ({M1_ANCHOR_PROJECT}); M1 said {M1_BASELINE_SHARE * 10:.0f}/10"
    )

    out = Path(os.environ.get("RELEVANCE_OUT", DEFAULT_OUT))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "m1_baseline_share": M1_BASELINE_SHARE,
                "target_share": TARGET_SHARE,
                "judge_live_calls": live_calls,
                "judge_agrees_with_m1_hand_rating": f"{agreed}/{len(anchor)}",
                "summary": summary,
                "picks": [asdict(pick) for pick in scored],
            },
            indent=2,
        )
    )
    for arm, row in summary.items():
        print(
            f"{arm:20s} {row['strong']:3d}/{row['scenes']:<3d} strongly on topic "
            f"= {float(row['share']) * 10:.1f}/10  "
            f"(generic {row['generic']}, wrong {row['wrong']})"
        )

    shipped = summary.get("metadata")
    before = summary.get("before")
    assert shipped is not None and before is not None, "an arm produced no scenes at all"
    assert int(shipped["scenes"]) >= len(TOPICS) * 5, "too few scenes to mean anything"
    # The ruler has to still be the ruler. If a rubric edit stops reproducing the one
    # hand rating that exists, every number below it is uncalibrated and meaningless.
    assert agreed >= JUDGE_AGREEMENT_FLOOR * len(anchor), (
        f"the judge now agrees with M1's hand rating on only {agreed}/{len(anchor)} "
        "scenes; recalibrate the rubric before trusting anything else here"
    )
    # A regression floor against the recorded measurement, and deliberately not
    # against `before`: the two are a fifth of a standard error apart, so asserting
    # an improvement over the M1 path would be asserting noise. Whether the target
    # was met is a question for `docs/visual-relevance-baseline.md`, and the answer
    # there is no.
    assert float(shipped["share"]) >= MEASURED_SHIPPED_SHARE - REGRESSION_TOLERANCE, (
        f"the shipped default scored {float(shipped['share']):.2f}, below the "
        f"{MEASURED_SHIPPED_SHARE:.2f} this harness recorded; visual search has "
        "regressed"
    )
    print(
        f"{'target':20s} {TARGET_SHARE * 10:.0f}/10 "
        f"({'MET' if float(shipped['share']) >= TARGET_SHARE else 'NOT MET'})"
    )


# ------------------------------------------------- what a *working* re-rank would buy


#: **Both defects this probe used to hand-patch are now fixed in the source.**
#: `GEMINI_MODEL_PREFERENCE` led with `gemini-2.5-flash`, which `ListModels` still
#: advertises and `generateContent` answers with `404 ... no longer available to new
#: users`; and `MAX_SCORE_TOKENS = 256` truncated a reasoning model mid-array. The
#: preference list now names ids measured to answer, `_with_model_fallback` retires
#: any that 404 rather than trusting the catalogue, and the token ceiling is derived
#: from `MAX_CANDIDATES` with reasoning headroom. So the probe runs the shipped
#: provider unpatched — which is the only version of it worth measuring.

#: Gemini's soft budget is 240 requests a day, shared with the script stage, and the
#: broken arm spends one per gated scene before failing. Four projects is what is
#: left to answer the question with, and it is enough to be directional.
REPAIRED_PROJECT_LIMIT = 4


def test_vision_rerank_repaired_probe(
    keys: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Would Task 13's re-rank be worth its quota *if it worked*?

    Written while the provider was broken, and it hand-patched the two constants to
    ask the question anyway. Those patches are gone: the model preference and the
    token ceiling are fixed in `providers/vision/gemini.py`, so this now measures
    the shipped provider. What it answers is the question the broken arm could not:
    with a model the account can reach and room to finish the sentence, does looking
    at the picture change the pick, and does it change it for the better?

    Deliberately narrow — four projects, on whatever is left of the day's Gemini
    allowance — and reported as directional, not as a number to put beside the
    hundred-scene arms.
    """
    monkeypatch.setattr(PexelsProvider, "download", _stub_download)

    # `_rerank` swallows every failure, `QuotaExceeded` included, so a spent budget
    # would show up here as "the re-rank moved nothing" — the exact conclusion the
    # broken arm produces, for a completely different reason. Refuse to run rather
    # than report that. On the day this was written the budget was gone precisely
    # because the broken re-rank had spent all 240 of it, twice, on 404s.
    try:
        QuotaTracker(USER_CACHE_ROOT / "quota.json").check("gemini", SOFT_BUDGETS["gemini"])
    except QuotaExceeded as exc:
        pytest.skip(f"no Gemini budget left to probe with: {exc}")

    topics = TOPICS[:REPAIRED_PROJECT_LIMIT]
    scripted = tmp_path / "scripted"
    write_scripts(scripted, topics)
    baseline = run_after_arm(
        scripted, tmp_path / "metadata", arm="metadata", rerank=False, topics=topics
    )
    repaired = run_after_arm(
        scripted, tmp_path / "repaired", arm="vision", rerank=True, topics=topics
    )

    scored, _live = score_all(baseline + repaired, build_judge(keys))

    by_arm = {arm: [p for p in scored if p.arm == arm] for arm in ("metadata", "vision")}
    moved = [
        (before, after)
        for before, after in zip(by_arm["metadata"], by_arm["vision"], strict=True)
        if before.source_id != after.source_id
    ]
    print(
        f"repaired re-rank: {len(moved)} of {len(by_arm['metadata'])} picks moved; "
        f"metadata {share_strongly_on_topic(by_arm['metadata']) * 10:.1f}/10 -> "
        f"vision {share_strongly_on_topic(by_arm['vision']) * 10:.1f}/10"
    )
    for before, after in moved:
        print(f"  {before.project[:30]}/{before.scene}: {before.score} -> {after.score}")

    out = Path(os.environ.get("RELEVANCE_OUT", DEFAULT_OUT)).with_name("vision-repaired.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps([asdict(pick) for pick in scored], indent=2))

    # The point of the probe: a re-rank that never moves a pick cannot be worth a
    # request, whatever it scores. This is what the broken arm silently failed.
    assert moved, "the repaired re-rank changed nothing — it is not worth its quota"
