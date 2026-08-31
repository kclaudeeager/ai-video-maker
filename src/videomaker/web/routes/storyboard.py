"""Gate 2: looking at every scene's shot, swapping it, and approving the storyboard.

**This module carries the M2 definition of done: editing one scene here
re-generates only that scene.** Everything below is arranged around that one
sentence, and `tests/unit/test_web_storyboard.py` proves it by counting
`run_ffmpeg` across a real re-run.

The reason it works is that nothing here invalidates anything. A swap writes one
field — `scene.visual.chosen` — and M1's own fingerprints do the rest:
`assemble` hashes each segment by its filter graph and its asset's *bytes*, so a
different asset restages exactly one segment; the join, the narration bed and the
final render follow because their inputs moved; and `visuals` stays current
because its hash covers the query and the kind, neither of which a swap touches.
The one thing this module must never do is reach into the stage cache and drop a
prefix — that would be a second, competing idea of what an edit invalidates, and
the first thing it would do is re-encode the whole video for a one-scene change.

**Choosing has to download.** M1's visuals stage downloads only the hit it picked,
so `visual.candidates[1:]` come back with `local_path == ""` — the *usual* state
of every alternative on this page. `_fetch_candidate` therefore re-runs the same
stock search the stage ran, finds the candidate by `source_id` and downloads it
before anything is written. It lands at `asset.<n>.<ext>` beside the stage's own
`asset.<ext>`: the previous shot stays on disk (swapping back is then free) and
the name still matches the `asset.*` glob the visuals stage sweeps when it
re-fetches, so the alternates cannot outlive the query that found them.

The download happens **outside the project lock** — it is network I/O, and
`run_pipeline` may be holding that lock for the length of a render — and the
decision is then re-taken inside the lock against a freshly read project, so a
swap can never clobber what the worker thread did in between.

**Searching does not rewrite `scene.visual.query`.** It replaces
`visual.candidates` and nothing else, and that restraint is the only reason the
button is safe to press: `query` is an input to M1's `visuals` fingerprint, so
moving it would make the scene stale and the very next `run_pipeline` would
re-fetch the scene, replacing both the alternatives just found and the shot
picked from them. A search that undoes itself is worse than no search. The
alternatives are review state; the query is pipeline state.

**The quota indicator is not decoration.** Pexels' soft budget is 190 requests an
hour and browsing a storyboard spends it fast, so the price of a search is
printed next to the search box and the remaining headroom rides back inside the
gate card on every reply. The thing that makes browsing affordable at all is
M1's `ResponseCache`: an identical query is answered from disk, issues no HTTP
request and therefore books nothing against the budget. `search` deliberately
adds no caching of its own — it calls the same `stock.search` the visuals stage
calls, so there is exactly one cache to reason about.

**The vertical controls decide things no stage can.** A Short is not a smaller
copy of the wide video: it is a *re-frame* (M3 Task 4's `reframe_filter` cuts a
9:16 window out of the source asset) and a *shorter edit* (`aspect_scenes` keeps
only the `in_short` subset). Which part of a 16:9 shot survives that crop, and
which scenes are worth the three minutes a Short is allowed, are both editorial
judgements, so both are controls here rather than heuristics in the pipeline.

The overlay drawn over each shot is the *real* crop window — `min(iw, ih·w/h)`
from `reframe_filter`, offset by `crop_focus_x` — computed from the asset's own
dimensions, so the shot is shown at its full source ratio rather than pre-cropped
to 16:9. An overlay drawn on an already-cropped picture would be a confident lie
about what the Short keeps. The slider commits on release (`change`, not
`input`), and the overlay follows the drag in the browser with no request at all;
see `storyboard.html`.

The running Short duration is drawn *inside the gate card*, for the same reason
the quota indicator is: every reply already swaps that card out of band, so one
readout stays in step with every toggle without a second out-of-band element.
`short_fits` is a duration test and nothing else, so a project with nothing
ticked runs 0 s and "fits" vacuously — `ShortView.state` therefore has four
values, and an empty Short reads as a problem rather than as a tick.

The fourth is `long`, and the distinction it draws is the point of M3 Task 22.
`MAX_SHORT_S` is the **platform limit** and gate 3 refuses on it; `SHORT_TARGET_S`
is the **editorial target**, where Shorts engagement actually peaks, and nothing
refuses on it anywhere. A cut between the two is legal on every platform and
competitive on none, so the readout says so and names the longest scenes — advice
with something to do attached. It never disables the approve button: trimming a
Short to a clock ends it mid-sentence, which is why the lever is the per-scene
tick and why the decision stays the owner's.

Everything else is the shape gate 1 established: compare first and lock second so
a no-op writes nothing; answer htmx with the card partial and a plain form post
with a 303; carry the gate card back out of band so a cleared preview approval is
news the page hears about without a reload; and never run a stage in a handler
(decision 2) — re-voice and approve both enqueue.

The gate view model is deliberately a local copy of gate 1's rather than an
import: the two pages are edited independently, and a shared 30-line dataclass is
not worth coupling their modules over. The *contract* they share is the
`data-gate`/`data-approved` attribute pair, which the tests read on both.
"""

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from videomaker.cache import StageCache, stage_key
from videomaker.config import Settings
from videomaker.models import Aspect, AssetRef, Motion, Project, Scene, StockResult, VisualKind
from videomaker.pipeline.assemble import (
    MAX_SHORT_S,
    SHORT_TARGET_S,
    VERTICAL_SPEC,
    VideoSpec,
    short_duration_s,
    timeline_scene_ids,
)
from videomaker.pipeline.base import ADVANCE_ON, StageDeps, call_chain
from videomaker.pipeline.visuals import (
    ASSET_STEM,
    MAX_CANDIDATES,
    MAX_HEIGHT,
    ORIENTATION,
    PROVIDER_KIND,
    NoResults,
    asset_suffix,
    resolve_kinds,
)
from videomaker.project import ProjectStore
from videomaker.providers.base import StockProvider
from videomaker.providers.errors import ProviderError
from videomaker.providers.ratelimit import SOFT_BUDGETS, Budget, QuotaTracker
from videomaker.runner import (
    GATE_BEFORE,
    STAGE_UNITS,
    build_deps,
    clear_stale_approvals,
    derive_status,
    run_pipeline,
    stage_cache_for,
    status_key,
)
from videomaker.templates import load_template

# The shared 404 helper, the status pill palette, the gate titles and the "run to
# the next gate" job body all belong to the dashboard module. Importing them keeps
# one definition of each.
from videomaker.web.guidance import next_step
from videomaker.web.routes.projects import (
    _STATUS_TONES,
    GATE_TITLES,
    _load,
    advance_job,
)
from videomaker.web.worker import JobFn, JobProgress, JobQueue, JobQueueFull

router = APIRouter()

#: The gate this page clears. `GATE_BEFORE` puts it in front of `captions`.
GATE = "storyboard"

SAVED_NOTE = "updated"
UNCHANGED_NOTE = "no change"

#: How a failed search is worded in the card. The provider's own sentence is kept
#: verbatim — `QuotaExceeded` already says which budget is spent, over which
#: window, and inventing a friendlier paraphrase would only hide that.
PROBLEM_NOTE = "That search did not run: {detail}"

#: Which window each `Budget` axis measures, for the indicator's labels.
WINDOW_LABELS: dict[str, str] = {
    "rpm": "this minute",
    "per_hour": "this hour",
    "per_day": "today",
}

#: Cloudflare bills neurons, everyone else bills requests (M0 finding 6).
UNIT_LABELS: dict[str, str] = {"cloudflare": "neurons"}
DEFAULT_UNIT = "requests"

#: Below this share of the budget the row goes amber: enough warning to finish
#: reviewing the storyboard, not so early that it cries wolf all session.
LOW_FRACTION = 0.2

#: The motions the picker offers, in `Motion`'s own order so a value added to the
#: enum appears here without a second list to remember.
MOTIONS: tuple[Motion, ...] = tuple(Motion)

#: What each motion does, for the option labels. Presentation only.
MOTION_NOTES: dict[Motion, str] = {
    Motion.PAN: "drift across the frame",
    Motion.ZOOM: "slow push in",
    Motion.NONE: "hold still",
}

#: The slider's granularity, and the precision `crop_focus_x` is stored at.
#: Quantising is what makes the no-op guard exact: a value the user has not moved
#: comes back byte-identical to the one on disk, so it never reaches the lock.
#: Two decimals is finer than the eye can place a crop and coarser than the
#: `%.3f` `reframe_filter` prints, so nothing is lost downstream.
CROP_STEP = 0.01
CROP_PLACES = 2

#: The values an HTML checkbox can send for "ticked". An unticked box sends *no*
#: field at all — unlike gate 1's textareas, where a blank means "leave it alone",
#: absence here genuinely is the off state, because that is what a checkbox means.
CHECKED_VALUES = frozenset({"on", "true", "1", "yes"})


# ------------------------------------------------------------------ view models


@dataclass(frozen=True)
class GateState:
    """One review gate as this page shows it. Derived from `project.approvals`."""

    name: str
    title: str
    approved_at: datetime | None

    @property
    def approved(self) -> bool:
        return self.approved_at is not None

    @property
    def marker(self) -> str:
        return "true" if self.approved else "false"

    @property
    def state_label(self) -> str:
        return "approved" if self.approved else "not approved"

    @property
    def tone(self) -> str:
        return "status-ok" if self.approved else "status-waiting"


@dataclass(frozen=True)
class Candidate:
    """One shot the visuals stage found, as a tile on the page.

    `media_url` is empty for the common case — a hit that was offered but never
    downloaded — and the tile then falls back to `preview_url`, the provider's own
    thumbnail, loaded straight from the provider. Only a ref old enough to predate
    `AssetRef.preview_url`, or one from a generator with no hosted copy, has
    neither; that is what the labelled placeholder is for.
    """

    index: int
    ref: AssetRef
    chosen: bool
    media_url: str

    @property
    def marker(self) -> str:
        return "true" if self.chosen else "false"

    @property
    def is_video(self) -> bool:
        return self.ref.duration_s is not None

    @property
    def downloaded(self) -> bool:
        return bool(self.media_url)

    @property
    def preview_url(self) -> str:
        """The provider's thumbnail — the only picture of a shot with no local file."""
        return self.ref.preview_url

    @property
    def kind_label(self) -> str:
        return "clip" if self.is_video else "still"

    @property
    def label(self) -> str:
        parts = [self.kind_label, f"{self.ref.width}×{self.ref.height}"]
        if self.ref.duration_s is not None:
            parts.append(f"{self.ref.duration_s:.0f}s")
        return " · ".join(parts)


@dataclass(frozen=True)
class CropWindow:
    """Where the vertical frame lands on the source, as percentages of its width.

    The same geometry `assemble.reframe_filter` emits — `min(iw, ih·w/h)` slid by
    `crop_focus_x` — and not a picture of it. The overlay is the only preview of
    the Short there is before a vertical render exists, so an approximation here
    would be a confident lie about which half of the shot survives.
    """

    focus: float
    #: The crop's width as a percentage of the source's, so the template can size
    #: the overlay without knowing the asset's pixels.
    window_pct: float

    @property
    def offset_pct(self) -> float:
        """The overlay's left edge. `(iw-ow)·focus`, expressed the same way."""
        return (100.0 - self.window_pct) * self.focus

    @property
    def keeps_all(self) -> bool:
        """A source already at or narrower than 9:16 loses nothing to the crop."""
        return self.window_pct >= 100.0


def crop_window(ref: AssetRef, focus: float, spec: VideoSpec = VERTICAL_SPEC) -> CropWindow | None:
    """The vertical window on `ref`, or `None` when its dimensions are unusable.

    `None` rather than a guessed ratio: a ref with no recorded size (an ancient
    project, or a provider that did not say) cannot have an honest overlay drawn
    for it, and drawing a dishonest one is worse than drawing none.
    """
    if ref.width <= 0 or ref.height <= 0:
        return None
    window = min(ref.width, ref.height * spec.width / spec.height)
    return CropWindow(focus=focus, window_pct=window / ref.width * 100.0)


@dataclass(frozen=True)
class SceneCard:
    """One scene's shot, its alternatives, and what the runner thinks of them."""

    project_id: str
    number: int
    scene: Scene
    candidates: list[Candidate]
    voice_current: bool
    visual_current: bool
    #: Transient feedback from the action that just happened, if this is a reply.
    note: str = ""
    #: Why the action that just happened did nothing — a spent budget, or a search
    #: that found no footage. Transient, like `note`.
    problem: str = ""
    #: The words the alternatives below were found with, when this reply came from
    #: a search. Not persisted: see the module docstring on `visual.query`.
    search_query: str = ""

    @property
    def chosen(self) -> Candidate | None:
        return next((candidate for candidate in self.candidates if candidate.chosen), None)

    @property
    def motions(self) -> tuple[Motion, ...]:
        return MOTIONS

    @property
    def motion_notes(self) -> dict[Motion, str]:
        return MOTION_NOTES

    @property
    def is_video(self) -> bool:
        chosen = self.chosen
        return chosen is not None and chosen.is_video

    @property
    def state_label(self) -> str:
        if not self.visual_current:
            return "needs a visual"
        return "shot chosen"

    @property
    def state_tone(self) -> str:
        return "status-ok" if self.visual_current else "status-waiting"

    @property
    def note_tone(self) -> str:
        return "status-ok" if self.note == SAVED_NOTE else ""

    @property
    def crop_focus_x(self) -> float:
        return self.scene.visual.crop_focus_x

    @property
    def crop_step(self) -> float:
        """The slider's granularity — the same grid `quantise_focus` stores on.

        Read from the constant rather than typed into the template, so the input
        the browser offers and the precision the handler keeps cannot drift.
        """
        return CROP_STEP

    @property
    def crop(self) -> CropWindow | None:
        """The 9:16 overlay for the shot in use, or `None` when there is nothing to draw.

        Measured from `scene.visual.chosen` rather than from the matching tile:
        the chosen ref is the one `assemble` hands to `reframe_filter`, and it is
        only *normally* a copy of the candidate. Where they disagree, the overlay
        has to describe the file the Short will actually be cut from.
        """
        ref = self.scene.visual.chosen
        return None if ref is None else crop_window(ref, self.crop_focus_x)

    @property
    def source_ratio(self) -> str:
        """The shot's own aspect ratio, as a CSS `aspect-ratio` value.

        The stage box is the *source's* shape, not 16:9, because the overlay has
        to sit on the whole frame the crop will be taken from. 16:9 is the
        fallback for a shot with no recorded size — the same case `crop` refuses
        to draw an overlay for.
        """
        ref = self.scene.visual.chosen
        if ref is None or ref.width <= 0 or ref.height <= 0:
            return "16 / 9"
        return f"{ref.width} / {ref.height}"

    @property
    def in_short(self) -> bool:
        return self.scene.in_short

    @property
    def in_short_marker(self) -> str:
        return "true" if self.in_short else "false"

    @property
    def short_seconds(self) -> float | None:
        """What this scene adds to the Short, or `None` until it has been voiced."""
        return self.scene.duration_s

    @property
    def candidates_query(self) -> str:
        """What the alternatives below were actually found with."""
        return self.search_query or self.scene.visual.query

    @property
    def summary(self) -> str:
        """The narration, short enough to identify the scene without re-reading it."""
        words = self.scene.narration.split()
        return " ".join(words[:16]) + ("…" if len(words) > 16 else "")


@dataclass(frozen=True)
class QuotaRow:
    """One provider's headroom on one window, as the indicator draws it."""

    provider: str
    axis: str
    remaining: int
    limit: int

    @property
    def window_label(self) -> str:
        return WINDOW_LABELS.get(self.axis, self.axis)

    @property
    def unit(self) -> str:
        return UNIT_LABELS.get(self.provider, DEFAULT_UNIT)

    @property
    def spent(self) -> bool:
        return self.remaining == 0

    @property
    def low(self) -> bool:
        return self.remaining <= self.limit * LOW_FRACTION

    @property
    def tone(self) -> str:
        if self.spent:
            return "status-failed"
        return "status-waiting" if self.low else "status-ok"


@dataclass(frozen=True)
class QuotaView:
    """Every soft budget's remaining headroom, plus what a search costs."""

    project_id: str
    rows: list[QuotaRow]
    #: The provider a search would actually hit — the head of the stock chain.
    search_provider: str


def clock(seconds: float) -> str:
    """`m:ss`, the way a video length is read out. Rounded to the nearest second."""
    whole = max(round(seconds), 0)
    return f"{whole // 60}:{whole % 60:02d}"


@dataclass(frozen=True)
class ShortView:
    """The `in_short` subset measured against **two** numbers that are not the same.

    * `limit_s` (`MAX_SHORT_S`, three minutes) is the **platform limit**. Gate 3
      refuses to render past it. Over it the readout is an error: state `over`.
    * `target_s` (`SHORT_TARGET_S`, 45 s) is the **editorial target** — where Shorts
      engagement actually peaks. Nothing refuses on it, here or anywhere else. Over
      it the readout is a nudge: state `long`, and it names the longest scenes so
      the nudge comes with something to do.

    **Four states, not a boolean.** `assemble.short_fits` is a duration test and
    says so: a project with nothing ticked runs for 0 s and passes it vacuously.
    Reporting that as a tick would hide an empty Short behind a green pill until
    gate 3 refused to render it, so `empty` is its own answer.
    """

    duration_s: float
    limit_s: float
    #: The scenes actually on the vertical timeline: ticked *and* voiced.
    included: list[str]
    #: Ticked but not yet voiced, so not on the timeline. The difference between
    #: "you have not chosen anything" and "the run has not caught up".
    pending: list[str]
    #: The longest scenes in the Short, longest first — what to untick when it
    #: overruns. Naming them is the difference between a complaint and an action.
    longest: list[tuple[str, float]]
    #: Advisory only. See the class docstring: nothing may gate on this.
    target_s: float = SHORT_TARGET_S

    @property
    def fits(self) -> bool:
        """Against the **limit**. The one test anything downstream refuses on."""
        return self.duration_s <= self.limit_s

    @property
    def meets_target(self) -> bool:
        """Against the **target**. Read for advice; never for permission."""
        return self.duration_s <= self.target_s

    @property
    def empty(self) -> bool:
        return not self.included

    @property
    def state(self) -> str:
        if self.empty:
            return "empty"
        if not self.fits:
            return "over"
        return "ok" if self.meets_target else "long"

    @property
    def tone(self) -> str:
        if self.state == "ok":
            return "status-ok"
        # `long` is advice, so it wears the waiting colour, not the failure one.
        return "status-waiting" if self.state == "long" else "status-failed"

    @property
    def names_scenes_to_drop(self) -> bool:
        """Both long states end in the same action: untick something."""
        return self.state in {"over", "long"}

    @property
    def over_s(self) -> float:
        return max(self.duration_s - self.limit_s, 0.0)

    @property
    def duration_label(self) -> str:
        return clock(self.duration_s)

    @property
    def limit_label(self) -> str:
        return clock(self.limit_s)

    @property
    def target_label(self) -> str:
        return clock(self.target_s)

    @property
    def over_label(self) -> str:
        return clock(self.over_s)

    @property
    def summary(self) -> str:
        if self.state == "empty":
            if self.pending:
                return "Every scene in the Short is still waiting to be voiced."
            return "No scene is in the Short yet — tick at least one."
        if self.state == "over":
            return f"{self.over_label} over the {self.limit_label} limit. Untick a scene."
        if self.state == "long":
            return (
                f"Past the {self.target_label} mark where Shorts hold attention. "
                "Nothing is blocked — only the limit refuses — but a tighter cut travels further."
            )
        return f"{self.duration_label} of {self.limit_label}."


def short_view(
    project: Project, limit_s: float = MAX_SHORT_S, target_s: float = SHORT_TARGET_S
) -> ShortView:
    """The Short's running time, straight from `assemble`'s own timeline.

    Measured with `short_duration_s` rather than by summing durations here: the
    number on this page has to be the one the vertical render will produce, gaps
    and all, and there is exactly one function that knows what that is.
    """
    included = timeline_scene_ids(project, Aspect.VERTICAL)
    on_timeline = set(included)
    durations = {
        scene.id: scene.duration_s or 0.0 for scene in project.scenes if scene.id in on_timeline
    }
    return ShortView(
        duration_s=short_duration_s(project),
        limit_s=limit_s,
        target_s=target_s,
        included=included,
        pending=[
            scene.id
            for scene in project.scenes
            if scene.in_short and scene.id not in on_timeline
        ],
        longest=sorted(durations.items(), key=lambda item: -item[1]),
    )


@dataclass(frozen=True)
class GateView:
    """Everything the gate 2 card renders, page and out-of-band reply alike."""

    project_id: str
    gate: GateState
    #: The gates either side of this one. Not "downstream": gate 1 sits *before*
    #: gate 2, and calling it downstream on this page would be a plain lie about
    #: the pipeline the whole UI is trying to explain.
    others: list[GateState]
    can_approve: bool
    #: The headroom indicator, drawn inside this card so one gate card carries it
    #: and every out-of-band reply refreshes it for free. `None` on the few
    #: callers that have no settings to build a tracker from.
    quota: QuotaView | None = None
    #: The Short's running time against the three-minute limit. Drawn in this card
    #: for the same reason the quota is: every reply already swaps it out of band,
    #: so one readout keeps up with every `in_short` toggle on the page.
    short: ShortView | None = None
    #: True only when this edit really did invalidate a later approval.
    cleared: bool = False
    #: The reply swaps this section out of band; the page renders it in place.
    oob: bool = False


def media_url(project_id: str, relpath: str) -> str:
    """The `/media/...` URL for a project-relative path, or `""` if there is none.

    Quoted per segment, so a filename with a space or a `#` still resolves; the
    `/` separators are kept because `media.get_media` takes a whole path.
    """
    return f"/media/{project_id}/{quote(relpath)}" if relpath else ""


def _unit_currency(project: Project, stage_cache: StageCache, stage: str) -> dict[str, bool]:
    """Per-unit `stage_is_current`, so a scene reports only its own freshness."""
    return {
        unit.unit: unit.produced
        and not stage_cache.is_stale(status_key(stage, unit.unit), unit.fingerprint)
        for unit in STAGE_UNITS[stage](project)
    }


def _candidates(project_id: str, scene: Scene, root: Path) -> list[Candidate]:
    """Every offered shot, with the current one marked.

    Identity is `source_id`, not list position: `chosen` is a copy of the ref, and
    a re-run of the visuals stage can hand back the same hits in the same order
    with a different one downloaded.
    """
    chosen = scene.visual.chosen
    tiles: list[Candidate] = []
    for index, ref in enumerate(scene.visual.candidates):
        on_disk = bool(ref.local_path) and (root / ref.local_path).is_file()
        tiles.append(
            Candidate(
                index=index,
                ref=ref,
                chosen=chosen is not None and chosen.source_id == ref.source_id,
                media_url=media_url(project_id, ref.local_path) if on_disk else "",
            )
        )
    return tiles


def scene_cards(project: Project, stage_cache: StageCache, root: Path) -> list[SceneCard]:
    """Every scene, in order, with its shots and its freshness attached."""
    voice = _unit_currency(project, stage_cache, "voice")
    visuals = _unit_currency(project, stage_cache, "visuals")
    return [
        SceneCard(
            project_id=project.id,
            number=number,
            scene=scene,
            candidates=_candidates(project.id, scene, root),
            voice_current=voice.get(scene.id, False),
            visual_current=visuals.get(scene.id, False),
        )
        for number, scene in enumerate(project.scenes, start=1)
    ]


def gate_states(project: Project) -> list[GateState]:
    """The three gates in pipeline order, as `project.approvals` has them."""
    return [
        GateState(
            name=gate,
            title=GATE_TITLES.get(gate, gate.title()),
            approved_at=getattr(project.approvals, gate),
        )
        for gate in GATE_BEFORE.values()
    ]


def gate_view(
    project: Project,
    *,
    quota: QuotaView | None = None,
    cleared: bool = False,
    oob: bool = False,
) -> GateView:
    states = gate_states(project)
    return GateView(
        project_id=project.id,
        gate=next(state for state in states if state.name == GATE),
        others=[state for state in states if state.name != GATE],
        quota=quota,
        short=short_view(project),
        # Approving a storyboard with no chosen shot would send an unrenderable
        # project into `captions`; the button says why instead.
        can_approve=bool(project.scenes)
        and all(scene.visual.chosen is not None for scene in project.scenes),
        cleared=cleared,
        oob=oob,
    )


# ------------------------------------------------------------- quota headroom


def quota_rows(tracker: QuotaTracker) -> list[QuotaRow]:
    """Every soft budget's headroom, one row per limited axis.

    A read and only a read: `QuotaTracker.remaining` books nothing, which is the
    whole point of drawing this before the user spends rather than after.
    """
    rows: list[QuotaRow] = []
    for provider in sorted(SOFT_BUDGETS):
        budget: Budget = SOFT_BUDGETS[provider]
        for axis, left in tracker.remaining(provider, budget).items():
            limit = getattr(budget, axis)
            # `None` is "unlimited on this axis": there is no headroom to report.
            if left is None or limit is None:
                continue
            rows.append(QuotaRow(provider=provider, axis=axis, remaining=left, limit=limit))
    return rows


def quota_view(settings: Settings, project_id: str) -> QuotaView:
    """The indicator's model. `build_deps` is what knows where the ledger lives."""
    deps = build_deps(settings, project_id)
    chain = deps.chain("stock")
    return QuotaView(
        project_id=project_id,
        rows=quota_rows(deps.quota),
        search_provider=chain[0] if chain else "no stock provider",
    )


# ------------------------------------------------------------------- the page


@router.get("/projects/{project_id}/storyboard")
def storyboard_page(request: Request, project_id: str):
    """Gate 2: one card per scene — the shot, its alternatives, motion and re-voice."""
    project = _load(request, project_id)
    store: ProjectStore = request.app.state.store
    stage_cache = stage_cache_for(store, project_id)
    status = derive_status(project, stage_cache)

    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "storyboard.html",
        {
            "project": project,
            "cards": scene_cards(project, stage_cache, store.path_for(project_id)),
            "next": next_step(project, stage_cache),
            "gate": gate_view(project, quota=quota_view(request.app.state.settings, project_id)),
            "status": status,
            "status_label": status.value.replace("_", " "),
            "status_tone": _STATUS_TONES.get(status, ""),
        },
    )


# ------------------------------------------------------------------- replying


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"


def _scene_or_404(project: Project, scene_id: str) -> Scene:
    try:
        return project.scene_by_id(scene_id)
    except KeyError as missing:
        raise HTTPException(
            status_code=404, detail=f"no such scene: {project.id}/{scene_id}"
        ) from missing


def _reply(
    request: Request,
    project_id: str,
    scene_id: str,
    *,
    note: str,
    cleared: bool,
    problem: str = "",
    search_query: str = "",
):
    """The card partial plus an out-of-band gate card, or a 303 with no JavaScript.

    The no-JavaScript path loses `problem`, because a fragment is not an answer to
    a browser navigation. It is not silent: the redirect lands on the page, and the
    headroom indicator there is exactly what a spent budget makes obvious.
    """
    if not _is_htmx(request):
        return RedirectResponse(
            url=f"/projects/{project_id}/storyboard#scene-{scene_id}", status_code=303
        )

    store: ProjectStore = request.app.state.store
    project = _load(request, project_id)
    stage_cache = stage_cache_for(store, project_id)
    cards = scene_cards(project, stage_cache, store.path_for(project_id))
    card = next(card for card in cards if card.scene.id == scene_id)

    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "_scene_card.html",
        {
            "project": project,
            "card": replace(card, note=note, problem=problem, search_query=search_query),
            "gate": gate_view(
                project,
                quota=quota_view(request.app.state.settings, project_id),
                cleared=cleared,
                oob=True,
            ),
        },
    )


# --------------------------------------------------------- choosing a candidate


def _fetch_candidate(
    settings: Settings, project: Project, scene: Scene, candidate: AssetRef, index: int
) -> AssetRef:
    """Download an offered-but-never-fetched candidate, and say where it landed.

    The same search the visuals stage ran, replayed so the provider hands back a
    `StockResult` it recognises — a bare `AssetRef` has no download URL, and only
    the library that produced a result can fetch it. The candidate is matched by
    `source_id`, so a provider that has reshuffled its page since is a miss rather
    than the wrong clip.
    """
    deps = build_deps(settings, project.id)
    scene_dir = deps.store.scene_dir(project, scene.id)
    kinds = resolve_kinds(scene, load_template(project.template).visual_kind_order)

    problems: list[str] = []
    for kind in kinds:
        if PROVIDER_KIND[kind] != "stock":
            # AI images are generated one at a time, so they never offer an
            # alternative there could be anything to download.
            continue
        try:
            return _download_from_stock(deps, scene, candidate, kind, scene_dir, index)
        except ProviderError as exc:
            problems.append(f"{kind.value}: {exc}")

    detail = "; ".join(problems) or "this scene's visual has no stock source to re-fetch from"
    raise HTTPException(status_code=502, detail=f"could not fetch that shot: {detail}")


def _download_from_stock(
    deps: StageDeps,
    scene: Scene,
    candidate: AssetRef,
    kind: VisualKind,
    scene_dir: Path,
    index: int,
) -> AssetRef:
    def call(name: str, stock: StockProvider) -> AssetRef:
        results = stock.search(
            query=scene.visual.query,
            kind=kind,
            min_duration_s=scene.duration_s or 0.0,
            orientation=ORIENTATION,
            per_page=MAX_CANDIDATES,
        )[:MAX_CANDIDATES]
        match = next(
            (result for result in results if result.source_id == candidate.source_id), None
        )
        if match is None:
            raise NoResults(f"{name} no longer offers {candidate.source_id}")
        # `asset.<n>.<ext>`, not `asset_<n>`: the visuals stage sweeps `asset.*`
        # when it re-fetches, so an alternate cannot outlive the query behind it.
        out_path = scene_dir / f"{ASSET_STEM}.{index}{asset_suffix(match)}"
        return stock.download(match, out_path, max_height=MAX_HEIGHT)

    return call_chain(deps, "stock", call, advance_on=(*ADVANCE_ON, NoResults))


def _needs_choosing(scene: Scene, wanted: AssetRef, root: Path) -> bool:
    """True when this really is a change — the guard that keeps a no-op a no-op.

    Also true when the right candidate is already chosen but its file has gone
    missing: re-picking it is then the obvious way to ask for it back.
    """
    chosen = scene.visual.chosen
    if chosen is None or chosen.source_id != wanted.source_id:
        return True
    return not chosen.local_path or not (root / chosen.local_path).is_file()


def _candidate_or_422(scene: Scene, index: int) -> AssetRef:
    candidates = scene.visual.candidates
    if not 0 <= index < len(candidates):
        raise HTTPException(
            status_code=422,
            detail=f"scene {scene.id} has {len(candidates)} candidates, not {index + 1}",
        )
    return candidates[index]


@router.post("/projects/{project_id}/scenes/{scene_id}/choose")
def choose_candidate(
    request: Request,
    project_id: str,
    scene_id: str,
    candidate_index: int = Form(0, ge=0),
):
    """Make one of the offered shots this scene's chosen visual.

    The download is done before the lock is taken and the choice is re-decided
    inside it, so a fetch that takes seconds never holds the project against the
    worker thread and never overwrites a change made while it was in flight.
    """
    store: ProjectStore = request.app.state.store
    # Both 404s happen *before* `store.lock`, which would otherwise create the
    # project folder for an id that does not exist.
    project = _load(request, project_id)
    scene = _scene_or_404(project, scene_id)
    wanted = _candidate_or_422(scene, candidate_index)
    root = store.path_for(project_id)

    note = UNCHANGED_NOTE
    cleared = False

    if _needs_choosing(scene, wanted, root):
        fetched = wanted
        if not wanted.local_path or not (root / wanted.local_path).is_file():
            fetched = _fetch_candidate(
                request.app.state.settings, project, scene, wanted, candidate_index
            )

        with store.lock(project_id):
            # Re-read under the lock: everything above was a snapshot.
            project = _load(request, project_id)
            scene = _scene_or_404(project, scene_id)
            fresh = _candidate_or_422(scene, candidate_index)
            if fresh.source_id != wanted.source_id:
                raise HTTPException(
                    status_code=409,
                    detail="the candidates changed while you were choosing; reload the page",
                )
            if _needs_choosing(scene, fresh, root):
                scene.visual.candidates[candidate_index] = fetched
                # A copy, so editing the candidate list later cannot silently
                # rewrite what the pipeline is building from.
                scene.visual.chosen = fetched.model_copy(deep=True)
                scene.error = None
                # M1 decides which approvals this invalidates; the UI reports it.
                cleared = clear_stale_approvals(project, stage_cache_for(store, project_id))
                store.save(project)
                note = SAVED_NOTE

    return _reply(request, project_id, scene_id, note=note, cleared=cleared)


# ------------------------------------------------------------- setting a motion


@router.post("/projects/{project_id}/scenes/{scene_id}/motion")
def set_motion(
    request: Request,
    project_id: str,
    scene_id: str,
    motion: str = Form(""),
):
    """Set how a still moves. Ignored for footage — `assemble` already knows that."""
    store: ProjectStore = request.app.state.store
    project = _load(request, project_id)
    scene = _scene_or_404(project, scene_id)
    try:
        wanted = Motion(motion)
    except ValueError as unknown:
        known = ", ".join(item.value for item in MOTIONS)
        raise HTTPException(
            status_code=422, detail=f"unknown motion {motion!r}; known motions: {known}"
        ) from unknown

    note = UNCHANGED_NOTE
    cleared = False

    # Compare first, lock second: the select fires on every change, including the
    # ones that put back what was already there.
    if scene.visual.motion is not wanted:
        with store.lock(project_id):
            project = _load(request, project_id)
            scene = _scene_or_404(project, scene_id)
            if scene.visual.motion is not wanted:
                scene.visual.motion = wanted
                cleared = clear_stale_approvals(project, stage_cache_for(store, project_id))
                store.save(project)
                note = SAVED_NOTE

    return _reply(request, project_id, scene_id, note=note, cleared=cleared)


# --------------------------------------------------- framing the vertical crop


def quantise_focus(value: float) -> float:
    """`crop_focus_x` as it is stored: on the slider's own 0.01 grid.

    The guard below compares floats for equality, and that is only safe because
    everything that reaches it has been through here. Without it, a slider parked
    where it started would post `0.5000000001` on some browser one day and start
    taking the lock — and the lock is the one a whole render holds.
    """
    return round(value, CROP_PLACES)


@router.post("/projects/{project_id}/scenes/{scene_id}/crop")
def set_crop_focus(
    request: Request,
    project_id: str,
    scene_id: str,
    crop_focus_x: float = Form(0.5, ge=0.0, le=1.0),
):
    """Point the 9:16 window at the part of the shot the Short should keep.

    0.0 frames the left edge of the source, 1.0 the right, 0.5 the centre —
    exactly what `assemble.reframe_filter` does with it. It also re-centres a
    still's pan (`assemble._pan_travel`), so this is a wide-video edit too, and
    the hint on the card says so.

    Compare first, lock second, like every other control here — more so, in fact:
    a drag across the slider can land several posts in a row, most of them putting
    back a value the scene already has.
    """
    store: ProjectStore = request.app.state.store
    project = _load(request, project_id)
    scene = _scene_or_404(project, scene_id)
    wanted = quantise_focus(crop_focus_x)

    note = UNCHANGED_NOTE
    cleared = False

    if scene.visual.crop_focus_x != wanted:
        with store.lock(project_id):
            project = _load(request, project_id)
            scene = _scene_or_404(project, scene_id)
            if scene.visual.crop_focus_x != wanted:
                scene.visual.crop_focus_x = wanted
                cleared = clear_stale_approvals(project, stage_cache_for(store, project_id))
                store.save(project)
                note = SAVED_NOTE

    return _reply(request, project_id, scene_id, note=note, cleared=cleared)


# -------------------------------------------------- keeping a scene in the Short


def _checked(value: str) -> bool:
    """Whether a checkbox field arrived ticked.

    An unticked checkbox sends no field at all, and FastAPI hands a missing
    `Form` its default, so `""` *is* the off state here. That is the opposite of
    gate 1's rule for a textarea, where a blank means "leave this alone" — and
    the two are both right, because a browser cannot send an empty textarea and
    an absent one differently, whereas an unticked box has no other way to speak.
    """
    return value.strip().lower() in CHECKED_VALUES


@router.post("/projects/{project_id}/scenes/{scene_id}/in_short")
def set_in_short(
    request: Request,
    project_id: str,
    scene_id: str,
    in_short: str = Form(""),
):
    """Keep this scene in the Short, or drop it from it.

    A Short is a shorter *edit*, not a truncation: `assemble.aspect_scenes` builds
    the vertical timeline from the ticked scenes in the project's own order, so
    dropping one here removes it cleanly rather than cutting mid-sentence at the
    three-minute mark. The gate card that rides back out of band carries the new
    running time, which is where the limit becomes visible.
    """
    store: ProjectStore = request.app.state.store
    project = _load(request, project_id)
    scene = _scene_or_404(project, scene_id)
    wanted = _checked(in_short)

    note = UNCHANGED_NOTE
    cleared = False

    # Compare first, lock second: re-ticking a ticked box must not queue behind a
    # render for the flock it does not need.
    if scene.in_short is not wanted:
        with store.lock(project_id):
            project = _load(request, project_id)
            scene = _scene_or_404(project, scene_id)
            if scene.in_short is not wanted:
                scene.in_short = wanted
                cleared = clear_stale_approvals(project, stage_cache_for(store, project_id))
                store.save(project)
                note = SAVED_NOTE

    return _reply(request, project_id, scene_id, note=note, cleared=cleared)


# ---------------------------------------------------------------- re-voicing one scene


def revoice_job(settings: Settings, project_id: str, scene_id: str) -> JobFn:
    """A job body that speaks one scene again and re-times it.

    The stage cache entries for that scene's `voice` and `align` units are
    dropped, which is the *only* thing that makes an unchanged narration
    re-synthesise: the stages skip a unit whose inputs have not moved, and the
    point of this button is that the take was bad rather than that the words
    were. The `status:` fingerprints are deliberately left alone — the project is
    not less current for having a new recording of the same sentence.

    Only `voice` and `align` are dropped, and only for this scene, so a re-voice
    costs one wav and one alignment.
    """

    def job(progress: JobProgress) -> None:
        deps = build_deps(settings, project_id)
        with deps.store.lock(project_id):
            for stage in ("voice", "align"):
                deps.stage_cache.invalidate(stage_key(stage, scene_id))
            deps.stage_cache.save()
        run_pipeline(deps.store.load(project_id), deps, until="align", on_stage=progress)

    return job


@router.post("/projects/{project_id}/scenes/{scene_id}/revoice")
def revoice_scene(request: Request, project_id: str, scene_id: str):
    """Enqueue a re-voice of one scene and redirect back to the card.

    Decision 2: nothing is spoken here. `JobQueue.submit` is a no-op while this
    project already has a job, so a double click cannot queue two.
    """
    project = _load(request, project_id)
    _scene_or_404(project, scene_id)

    jobs: JobQueue = request.app.state.jobs
    try:
        jobs.submit(
            project_id, "revoice", revoice_job(request.app.state.settings, project_id, scene_id)
        )
    except JobQueueFull as full:
        raise HTTPException(status_code=503, detail=str(full)) from full
    return RedirectResponse(
        url=f"/projects/{project_id}/storyboard#scene-{scene_id}", status_code=303
    )


# ------------------------------------------------------------ searching stock


def _downloaded_paths(scene: Scene, root: Path) -> dict[str, str]:
    """`source_id` -> project-relative path, for every shot already on disk.

    A search that turns up a hit this scene has downloaded before hands the offer
    its file back, so the tile shows a real thumbnail and picking it costs no
    second download. Swapping back has to stay free.
    """
    refs = [*scene.visual.candidates]
    if scene.visual.chosen is not None:
        refs.append(scene.visual.chosen)
    return {
        ref.source_id: ref.local_path
        for ref in refs
        if ref.local_path and (root / ref.local_path).is_file()
    }


def _offer(result: StockResult, known: dict[str, str]) -> AssetRef:
    """A hit that was found but not downloaded — `visuals._offer` plus the carry-over."""
    return AssetRef(
        provider=result.provider,
        source_id=result.source_id,
        source_url=result.source_url,
        local_path=known.get(result.source_id, ""),
        preview_url=result.preview_url,
        width=result.width,
        height=result.height,
        duration_s=result.duration_s,
        attribution=result.attribution,
        license=result.license,
    )


def _search_kind(
    deps: StageDeps, scene: Scene, query: str, kind: VisualKind, known: dict[str, str]
) -> list[AssetRef]:
    """One kind, down the whole stock chain. Cheap on a repeat: see `ResponseCache`."""

    def call(name: str, stock: StockProvider) -> list[AssetRef]:
        results = stock.search(
            query=query,
            kind=kind,
            # The same filter the visuals stage applies: an alternative too short
            # to cover this scene is not an alternative.
            min_duration_s=scene.duration_s or 0.0,
            orientation=ORIENTATION,
            per_page=MAX_CANDIDATES,
        )[:MAX_CANDIDATES]
        if not results:
            raise NoResults(f"{name} has no {kind.value} for {query!r}")
        return [_offer(result, known) for result in results]

    return call_chain(deps, "stock", call, advance_on=(*ADVANCE_ON, NoResults))


def _search_offers(
    settings: Settings, project: Project, scene: Scene, query: str, root: Path
) -> list[AssetRef]:
    """The alternatives `query` turns up, in the scene's own kind order.

    Nothing is downloaded: a search offers, and `choose` is what fetches. Raises
    `ProviderError` — including a wrapped `QuotaExceeded` — which the handler
    turns into a line in the card rather than a 500.
    """
    deps = build_deps(settings, project.id)
    known = _downloaded_paths(scene, root)
    kinds = [
        kind
        for kind in resolve_kinds(scene, load_template(project.template).visual_kind_order)
        if PROVIDER_KIND[kind] == "stock"
    ]
    if not kinds:
        raise ProviderError("this scene does not use stock footage, so there is nothing to search")

    problems: list[str] = []
    for kind in kinds:
        try:
            return _search_kind(deps, scene, query, kind, known)
        except ProviderError as exc:
            problems.append(f"{kind.value}: {exc}")
    raise ProviderError("; ".join(problems))


@router.post("/projects/{project_id}/scenes/{scene_id}/search")
def search_stock(
    request: Request,
    project_id: str,
    scene_id: str,
    query: str = Form(""),
):
    """Search stock for this scene and offer what came back as the alternatives.

    `Form("")` rather than `Form(...)`: FastAPI substitutes a `Form` default for
    any empty value, so a blank box and an absent field are indistinguishable by
    the time they arrive. Refusing both here with one message is the honest
    reading of that — the alternative, a required field, answers a blank box with
    "field required", which is not what happened. The `required` attribute on the
    input stops it ever being sent.

    The network call happens **outside the project lock** — `run_pipeline` may be
    holding it for a whole render — and the comparison that decides whether there
    is anything to write happens before the lock as well, so a repeated search
    neither writes nor blocks.
    """
    store: ProjectStore = request.app.state.store
    project = _load(request, project_id)
    scene = _scene_or_404(project, scene_id)
    wanted = query.strip()
    if not wanted:
        raise HTTPException(
            status_code=422,
            detail="a stock search needs a query; type what should be on screen",
        )

    root = store.path_for(project_id)
    note = UNCHANGED_NOTE
    problem = ""
    cleared = False
    try:
        offers = _search_offers(request.app.state.settings, project, scene, wanted, root)
    except ProviderError as exc:
        # A spent free tier is the expected failure here, not an exceptional one.
        # It belongs in the card, next to the headroom that explains it.
        offers = None
        note = ""
        problem = PROBLEM_NOTE.format(detail=exc)

    # Compare first, lock second. Re-searching the query the visuals stage already
    # ran rebuilds exactly the offers already stored, and that must cost nothing.
    if offers is not None and offers != scene.visual.candidates:
        with store.lock(project_id):
            project = _load(request, project_id)
            scene = _scene_or_404(project, scene_id)
            if offers != scene.visual.candidates:
                scene.visual.candidates = offers
                scene.error = None
                # Replacing the alternatives moves no stage input, so this cannot
                # clear anything today. It is called because *deciding* that is
                # M1's job, not this module's.
                cleared = clear_stale_approvals(project, stage_cache_for(store, project_id))
                store.save(project)
                note = SAVED_NOTE

    return _reply(
        request,
        project_id,
        scene_id,
        note=note,
        cleared=cleared,
        problem=problem,
        search_query=wanted,
    )


@router.get("/projects/{project_id}/quota")
def quota_panel(request: Request, project_id: str):
    """The headroom indicator on its own, so it can refresh as the window slides."""
    # A 404 for an unknown project, so a stale tab does not poll a ghost forever.
    _load(request, project_id)
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "_scene_card.html",
        {"quota": quota_view(request.app.state.settings, project_id)},
    )


# ------------------------------------------------------------------ approving


@router.post("/projects/{project_id}/approve/storyboard")
def approve_storyboard(request: Request, project_id: str):
    """Stamp gate 2, enqueue a run to the next gate, and redirect to the dashboard.

    Idempotent by construction: an existing stamp is left as it is, so a
    double-clicked button cannot rewrite the moment the human actually approved.
    """
    _load(request, project_id)
    store: ProjectStore = request.app.state.store
    with store.lock(project_id):
        project = _load(request, project_id)
        if getattr(project.approvals, GATE) is None:
            setattr(project.approvals, GATE, datetime.now(UTC))
            store.save(project)

    jobs: JobQueue = request.app.state.jobs
    try:
        jobs.submit(project_id, "run", advance_job(request.app.state.settings, project_id))
    except JobQueueFull as full:
        raise HTTPException(status_code=503, detail=str(full)) from full
    return RedirectResponse(url=f"/projects/{project_id}", status_code=303)
