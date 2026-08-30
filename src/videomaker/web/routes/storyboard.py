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
from videomaker.models import AssetRef, Motion, Project, Scene, VisualKind
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

#: The motions the picker offers, in `Motion`'s own order so a value added to the
#: enum appears here without a second list to remember.
MOTIONS: tuple[Motion, ...] = tuple(Motion)

#: What each motion does, for the option labels. Presentation only.
MOTION_NOTES: dict[Motion, str] = {
    Motion.PAN: "drift across the frame",
    Motion.ZOOM: "slow push in",
    Motion.NONE: "hold still",
}


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
    downloaded — and the template draws a labelled placeholder instead. There is
    nothing better to draw: `AssetRef` keeps no remote preview, and fetching one
    would mean a network round trip on every page render for a picture the user
    may never look at.
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
    def kind_label(self) -> str:
        return "clip" if self.is_video else "still"

    @property
    def label(self) -> str:
        parts = [self.kind_label, f"{self.ref.width}×{self.ref.height}"]
        if self.ref.duration_s is not None:
            parts.append(f"{self.ref.duration_s:.0f}s")
        return " · ".join(parts)


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
    def summary(self) -> str:
        """The narration, short enough to identify the scene without re-reading it."""
        words = self.scene.narration.split()
        return " ".join(words[:16]) + ("…" if len(words) > 16 else "")


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


def gate_view(project: Project, *, cleared: bool = False, oob: bool = False) -> GateView:
    states = gate_states(project)
    return GateView(
        project_id=project.id,
        gate=next(state for state in states if state.name == GATE),
        others=[state for state in states if state.name != GATE],
        # Approving a storyboard with no chosen shot would send an unrenderable
        # project into `captions`; the button says why instead.
        can_approve=bool(project.scenes)
        and all(scene.visual.chosen is not None for scene in project.scenes),
        cleared=cleared,
        oob=oob,
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
            "gate": gate_view(project),
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


def _reply(request: Request, project_id: str, scene_id: str, *, note: str, cleared: bool):
    """The card partial plus an out-of-band gate card, or a 303 with no JavaScript."""
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
            "card": replace(card, note=note),
            "gate": gate_view(project, cleared=cleared, oob=True),
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
