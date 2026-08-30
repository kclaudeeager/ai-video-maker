"""Gate 1: reading the script, editing it scene by scene, and approving it.

**The whole module turns on one rule: a save that changes nothing writes nothing.**

The page autosaves — htmx fires a POST a beat after the typing stops — so this
endpoint is hit constantly with text that is byte-identical to what is already on
disk. A naive handler would `store.save(project)` every time. That looks harmless
and is not: `save()` is an atomic `os.replace`, so every one of those posts
replaces `project.json`, takes the whole-project `flock` (possibly against a
render running on the worker thread), and hands the next `run_pipeline` a file it
has every reason to re-examine. The M1 promise that editing scene two re-voices
scene two and nothing else survives only if "editing" means *actually changing
something*. Hence `_edits`: it compares the normalised incoming values against the
stored ones and returns an empty dict when they agree, and the lock is only ever
opened around a write that will really happen.

Normalisation is part of that guard, not a nicety. HTML form submission converts
every newline in a `<textarea>` to CRLF, so without `_normalise` a browser's very
first autosave of untouched text would differ from the LF-normalised string the
script stage wrote, the guard would never once fire in real use, and the caching
promise would be quietly dead. `tests/unit/test_web_script_gate.py` pins both the
plain no-op and the CRLF one.

Everything else here is the usual M2 shape: the handler never runs a stage
(decision 2) — approving stamps the timestamp and *enqueues* — and no state is
stored that `runner` could derive (decision 3), so the freshness pill on each
scene is `STAGE_UNITS` read live, and the gate rows are `project.approvals`.

Editing after an approval is where M1's `clear_stale_approvals` earns its place:
rewriting a narration under a signed-off storyboard drops the storyboard and
preview approvals, and the response carries an out-of-band `#gate-state` update
so the page says so without a reload.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from videomaker.cache import StageCache
from videomaker.models import Project, Scene
from videomaker.project import ProjectStore
from videomaker.runner import (
    GATE_BEFORE,
    STAGE_UNITS,
    clear_stale_approvals,
    derive_status,
    stage_cache_for,
    status_key,
)
from videomaker.scenes import delete_scene, merge_scenes, reorder_scenes, split_scene

# `_load` (the shared 404 helper), the status pill palette and the "run to the
# next gate" job body all belong to the dashboard module. Importing them keeps
# one definition of each: a second 404 helper here would be a second chance to
# return a 500 for a URL somebody typed by hand.
from videomaker.web.routes.projects import (
    _STATUS_TONES,
    GATE_TITLES,
    _load,
    advance_job,
)
from videomaker.web.worker import JobQueue, JobQueueFull

router = APIRouter()

#: The gate this page clears, and the stage it guards.
GATE = "script"

#: How long htmx waits after the last keystroke before saving. Long enough that
#: typing a sentence is one request rather than forty; short enough that closing
#: the tab straight after an edit does not lose it.
AUTOSAVE_DELAY_MS = 800

SAVED_NOTE = "saved"
UNCHANGED_NOTE = "no change"


def _normalise(text: str) -> str:
    """The form's idea of the text as the project would store it.

    CRLF to LF because that is what HTML form submission does to a `<textarea>`,
    and a trim because trailing whitespace is never a meaningful edit. Without
    this the no-op guard below would never fire for a real browser.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


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
class SceneRow:
    """One scene's editor, plus what the runner currently thinks of its artefacts.

    `voice_current`/`align_current`/`visual_current` are read from `STAGE_UNITS`
    at render time, never remembered: they are the same numbers the dashboard's
    stepper and the CLI's `status` are working from (decision 3).
    """

    project_id: str
    number: int
    scene: Scene
    voice_current: bool
    align_current: bool
    visual_current: bool
    #: Every scene id in the project, in order. The move buttons post a whole new
    #: order rather than a direction, so `POST /scenes/reorder` never has to guess
    #: what "up" meant against a project that may have moved since this render.
    sibling_ids: tuple[str, ...] = ()
    #: Transient feedback from a save that just happened, if this row is a reply.
    note: str = ""

    @property
    def word_count(self) -> int:
        return len(self.scene.narration.split())

    @property
    def can_split(self) -> bool:
        """A one-word scene has no interior boundary to cut at."""
        return self.word_count > 1

    @property
    def max_split_word(self) -> int:
        return max(self.word_count - 1, 1)

    @property
    def default_split_word(self) -> int:
        return max(self.word_count // 2, 1)

    @property
    def next_id(self) -> str | None:
        """The scene this one would merge with, or None if it is the last."""
        index = self.number
        return self.sibling_ids[index] if index < len(self.sibling_ids) else None

    @property
    def can_move_up(self) -> bool:
        return self.number > 1

    @property
    def can_move_down(self) -> bool:
        return self.number < len(self.sibling_ids)

    @property
    def up_order(self) -> list[str]:
        return self._swapped(-1)

    @property
    def down_order(self) -> list[str]:
        return self._swapped(1)

    def _swapped(self, step: int) -> list[str]:
        """This scene's id moved one place, as a complete order to post back.

        Always a full permutation of the project's ids, even at the ends where the
        swap is a no-op: `reorder_scenes` rejects anything less, and it should —
        a partial list would be a silent delete.
        """
        ids = list(self.sibling_ids)
        here = self.number - 1
        there = here + step
        if 0 <= there < len(ids):
            ids[here], ids[there] = ids[there], ids[here]
        return ids

    @property
    def state_label(self) -> str:
        if not self.voice_current:
            return "needs voicing"
        if not self.align_current:
            return "needs timing"
        return "voiced"

    @property
    def state_tone(self) -> str:
        return "status-ok" if self.voice_current and self.align_current else "status-waiting"

    @property
    def note_tone(self) -> str:
        return "status-ok" if self.note == SAVED_NOTE else ""


def _unit_currency(project: Project, stage_cache: StageCache, stage: str) -> dict[str, bool]:
    """Per-unit `stage_is_current`, so a scene can report only its own freshness.

    `stage_is_current` answers for a whole stage; this page needs the same
    question one scene at a time. It is the same comparison, unit by unit, using
    the runner's own fingerprints — nothing is recomputed here.
    """
    return {
        unit.unit: unit.produced
        and not stage_cache.is_stale(status_key(stage, unit.unit), unit.fingerprint)
        for unit in STAGE_UNITS[stage](project)
    }


def scene_rows(project: Project, stage_cache: StageCache) -> list[SceneRow]:
    """Every scene, in order, with its voice/align/visual freshness attached."""
    voice = _unit_currency(project, stage_cache, "voice")
    align = _unit_currency(project, stage_cache, "align")
    visuals = _unit_currency(project, stage_cache, "visuals")
    return [
        SceneRow(
            project_id=project.id,
            number=number,
            scene=scene,
            voice_current=voice.get(scene.id, False),
            align_current=align.get(scene.id, False),
            visual_current=visuals.get(scene.id, False),
            sibling_ids=tuple(other.id for other in project.scenes),
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


def _gate_context(project: Project, *, cleared: bool = False, oob: bool = False) -> dict[str, object]:
    """Context for `_script_gate.html`, shared by the page and the save reply."""
    states = gate_states(project)
    return {
        "gate": next(state for state in states if state.name == GATE),
        "downstream": [state for state in states if state.name != GATE],
        "can_approve": bool(project.scenes),
        # True only for the reply to an edit that really did invalidate a later
        # gate, so the banner is news rather than wallpaper.
        "cleared": cleared,
        # The reply swaps this section out of band; the page renders it in place.
        "oob": oob,
    }


# ------------------------------------------------------------------- the page


@router.get("/projects/{project_id}/script")
def script_page(request: Request, project_id: str):
    """Gate 1: one textarea and one visual-query input per scene, plus approve."""
    project = _load(request, project_id)
    store: ProjectStore = request.app.state.store
    stage_cache = stage_cache_for(store, project_id)
    status = derive_status(project, stage_cache)

    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "script.html",
        {
            **_gate_context(project),
            "project": project,
            "scenes": scene_rows(project, stage_cache),
            "status": status,
            "status_label": status.value.replace("_", " "),
            "status_tone": _STATUS_TONES.get(status, ""),
            "autosave_delay_ms": AUTOSAVE_DELAY_MS,
        },
    )


# --------------------------------------------------- editing the scene list

# **Scene ids are stable and are not positions** (design decision 5). The
# arithmetic lives in `videomaker.scenes`, which is pure and separately tested;
# these four handlers are the adapter: 404 for an id that does not exist, 400 for
# an operation that does not make sense, and one write under one lock for
# everything else.
#
# Unlike `save_scene` there is no no-op guard, and there should not be: a
# structural operation is a deliberate button press that always changes the list,
# so the compare-first dance that autosave needs would only be ceremony here.


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"


def _scene_or_404(project: Project, scene_id: str) -> Scene:
    try:
        return project.scene_by_id(scene_id)
    except KeyError as missing:
        raise HTTPException(
            status_code=404, detail=f"no such scene: {project.id}/{scene_id}"
        ) from missing


def _following_id(project: Project, scene_id: str) -> str:
    """The id of the scene after `scene_id` — what "merge with next" means."""
    _scene_or_404(project, scene_id)
    ids = [scene.id for scene in project.scenes]
    index = ids.index(scene_id)
    if index + 1 >= len(ids):
        raise ValueError("the last scene has nothing after it to merge with")
    return ids[index + 1]


#: A scene operation: the project as it is on disk, plus the store and cache it
#: may need to keep the artefacts honest, in exchange for the project as it should
#: become.
SceneOp = Callable[[Project, ProjectStore, StageCache], Project]


def _restructure(request: Request, project_id: str, operate: SceneOp, *, anchor: str = ""):
    """Run one scene operation under the project lock and answer the browser.

    The project is loaded twice on purpose. The first load is the 404 — it happens
    *before* `store.lock`, which would otherwise create a folder for an id nobody
    has; the second is inside the lock, because the worker thread may have moved
    the project since the page that posted this was rendered.

    `KeyError` from the operation is an id the project does not have (404);
    `ValueError` is an operation that cannot mean anything — a split at word zero,
    an order that is not a permutation (400). Both are raised before anything is
    written or deleted, so a rejected operation leaves the project exactly as it
    was.
    """
    store: ProjectStore = request.app.state.store
    _load(request, project_id)

    with store.lock(project_id):
        project = _load(request, project_id)
        stage_cache = stage_cache_for(store, project_id)
        try:
            project = operate(project, store, stage_cache)
        except KeyError as missing:
            raise HTTPException(
                status_code=404, detail=f"no such scene: {project_id}/{missing.args[0]}"
            ) from missing
        except ValueError as bad:
            raise HTTPException(status_code=400, detail=str(bad)) from bad
        # Adding, removing or rewording a scene can invalidate a gate somebody has
        # already signed off; M1 decides which, exactly as it does for an edit.
        clear_stale_approvals(project, stage_cache)
        store.save(project)

    url = f"/projects/{project_id}/script"
    if anchor:
        url = f"{url}#scene-{anchor}"
    if _is_htmx(request):
        # Half the page has moved, so there is no honest partial to swap: tell
        # htmx to navigate. A plain form post gets the same destination as a 303.
        return Response(status_code=204, headers={"HX-Redirect": url})
    return RedirectResponse(url=url, status_code=303)


# **This route must be registered before `POST /projects/{id}/scenes/{scene_id}`.**
# Starlette matches in registration order, so the other way round every reorder
# arrives at `save_scene` as a scene called "reorder" and answers 404 forever.
@router.post("/projects/{project_id}/scenes/reorder")
def reorder(
    request: Request,
    project_id: str,
    order: list[str] = Form(default=[]),
    focus: str = Form(""),
):
    """Put the scenes in the posted order. Re-encodes nothing that belongs to a scene.

    The form posts the whole new order rather than "move s03 up", so the server
    never has to reconstruct an intent from a page that may be stale — and a
    stale order (one that is no longer a permutation of the project's ids) is
    rejected outright rather than applied to the wrong list.
    """
    return _restructure(
        request,
        project_id,
        lambda project, _store, _cache: reorder_scenes(project, list(order)),
        anchor=focus,
    )


@router.post("/projects/{project_id}/scenes/{scene_id}/split")
def split(request: Request, project_id: str, scene_id: str, at_word: int = Form(...)):
    """Cut a scene in two at a word boundary; the new half takes a fresh id."""
    return _restructure(
        request,
        project_id,
        lambda project, store, cache: split_scene(
            project, scene_id, at_word, store=store, stage_cache=cache
        ),
        anchor=scene_id,
    )


@router.post("/projects/{project_id}/scenes/{scene_id}/merge")
def merge(request: Request, project_id: str, scene_id: str, second_id: str = Form("")):
    """Join this scene with another — by default the one after it — keeping this id."""

    def operate(project: Project, store: ProjectStore, cache: StageCache) -> Project:
        second = second_id or _following_id(project, scene_id)
        return merge_scenes(project, scene_id, second, store=store, stage_cache=cache)

    return _restructure(request, project_id, operate, anchor=scene_id)


@router.post("/projects/{project_id}/scenes/{scene_id}/delete")
def delete(request: Request, project_id: str, scene_id: str):
    """Remove a scene and every artefact that was ever built for it."""
    return _restructure(
        request,
        project_id,
        lambda project, store, cache: delete_scene(
            project, scene_id, store=store, stage_cache=cache
        ),
    )


# ------------------------------------------------------------- saving a scene


def _edits(scene: Scene, *, narration: str, query: str) -> dict[str, str]:
    """The submitted values that actually differ from what is stored.

    **This is the no-op guard.** An empty result means the caller must not take
    the lock and must not write: re-voicing a scene because someone tabbed
    through its textarea is exactly the cost this exists to avoid.

    A blank field is "leave this alone", never "empty this". Two reasons, and
    they agree. First, autosave fires *mid-edit*: a textarea that is empty for
    the half second between select-all and the first replacement keystroke must
    not take the narration with it. Second, FastAPI substitutes a `Form`
    parameter's default for any empty value, so a submitted `""` and an absent
    field arrive here identically — an endpoint that treated them differently
    would be describing a distinction it cannot actually observe.
    """
    changes: dict[str, str] = {}
    text = _normalise(narration)
    if text and text != scene.narration:
        changes["narration"] = text
    text = _normalise(query)
    if text and text != scene.visual.query:
        changes["query"] = text
    return changes


def _apply(scene: Scene, changes: dict[str, str]) -> None:
    if "narration" in changes:
        scene.narration = changes["narration"]
    if "query" in changes:
        scene.visual.query = changes["query"]


@router.post("/projects/{project_id}/scenes/{scene_id}")
def save_scene(
    request: Request,
    project_id: str,
    scene_id: str,
    narration: str = Form(""),
    query: str = Form(""),
):
    """Save one scene's narration and/or visual query — or, far more often, don't.

    The lock is opened only around a write that is going to happen, and the
    project is re-read inside it so a save can never clobber a change the worker
    thread made between the page render and this post.

    htmx gets the updated scene-row partial (200) plus an out-of-band gate
    update; a plain browser form post gets a 303 back to the page, so the site
    still works with JavaScript off (M2 global constraint) instead of leaving a
    bare fragment on screen.
    """
    store: ProjectStore = request.app.state.store
    # Both 404s happen *before* `store.lock`, which would otherwise create the
    # project folder for an id that does not exist.
    project = _load(request, project_id)
    scene = _scene_or_404(project, scene_id)

    cleared = False
    note = UNCHANGED_NOTE

    # **Compare first, lock second.** A save with nothing to save must not so
    # much as reach for the flock: `run_pipeline` holds it for the length of a
    # whole render, so an autosave that locked unconditionally would park a
    # request thread for minutes every time somebody paused mid-sentence while
    # a render was going. This outer check is only an optimisation — its
    # correctness rests on the identical check inside the lock, which is the one
    # that decides.
    if _edits(scene, narration=narration, query=query):
        with store.lock(project_id):
            # Re-read under the lock: everything above was a snapshot, and the
            # worker thread may have moved the project since it was taken.
            project = _load(request, project_id)
            scene = _scene_or_404(project, scene_id)
            changes = _edits(scene, narration=narration, query=query)
            if changes:
                _apply(scene, changes)
                # M1 decides which approvals an edit invalidates; the UI only
                # reports it. Computed after the edit, against the same cache
                # the runner will consult.
                cleared = clear_stale_approvals(project, stage_cache_for(store, project_id))
                store.save(project)
                note = SAVED_NOTE

    if not _is_htmx(request):
        return RedirectResponse(
            url=f"/projects/{project_id}/script#scene-{scene_id}", status_code=303
        )

    project = _load(request, project_id)
    stage_cache = stage_cache_for(store, project_id)
    row = next(row for row in scene_rows(project, stage_cache) if row.scene.id == scene_id)

    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "_scene_saved.html",
        {
            **_gate_context(project, cleared=cleared, oob=True),
            "project": project,
            # `replace`, not a re-listing of the fields: a row built by hand here
            # would quietly lose whatever `scene_rows` learns to attach next.
            "row": replace(row, note=note),
            "autosave_delay_ms": AUTOSAVE_DELAY_MS,
        },
    )


# ------------------------------------------------------------------ approving


@router.post("/projects/{project_id}/approve/script")
def approve_script(request: Request, project_id: str):
    """Stamp gate 1, enqueue a run to the next gate, and redirect to the dashboard.

    Idempotent by construction: an existing stamp is left exactly as it is, so
    a double-clicked button (or a browser replaying the post) cannot rewrite the
    moment the human actually approved. The run is enqueued either way — the
    worker's own one-job-per-project rule makes a second submit a no-op.
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
