"""The project list, project creation, and one project's dashboard.

Two rules from the plan shape this module more than anything else:

**Status is derived, never stored** (decision 3). Every row calls
`runner.derive_status` against the project's own stage cache at render time.
Nothing is memoised, not even for the length of one request: a status cached
here would be a second source of truth, and the first thing it would do is
disagree with the CLI after a hand-edit of `project.json`.

**A request handler never runs a stage** (decision 2). `POST /projects` writes
the project folder — cheap, local, bounded — and then *enqueues* the script run
before redirecting. The stages are synchronous and `run_pipeline` holds a
blocking `fcntl.flock` for its whole duration, so running one here would block
the event loop for as long as a provider takes to answer.

The 303 on that redirect is the point of POST-then-redirect: it tells the client
to re-issue as GET, so the browser's back button and reload cannot resubmit the
form and create a second project.

**The dashboard polls, and stops.** `GET /projects/{id}/job` renders `_job.html`
carrying `hx-trigger="every 1500ms"` — but only while the job is still queued or
running. Once it reaches `done`, `failed` or `blocked` the attribute is omitted,
which is how the polling loop ends: htmx swaps in a fragment that no longer asks
to be swapped again. Without that, a tab left open on a finished project would go
on making forty requests a minute for as long as the browser is open, each one
re-reading `project.json` and the stage cache off disk. `_terminal` is the single
place that decision is made, and `tests/unit/test_web_dashboard.py` asserts both
halves of it.
"""

from dataclasses import dataclass, replace
from datetime import datetime

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from videomaker.cache import STAGE_ORDER, StageCache
from videomaker.config import Settings
from videomaker.models import Project, Status
from videomaker.project import ProjectStore
from videomaker.runner import (
    GATE_BEFORE,
    GATE_REVIEW,
    build_deps,
    derive_status,
    run_pipeline,
    stage_cache_for,
    stage_is_current,
)
from videomaker.templates import list_templates
from videomaker.web.guidance import next_step
from videomaker.web.onboarding import free_tier_rows, key_rows
from videomaker.web.voices import available_voices, refusal_for
from videomaker.web.worker import (
    BLOCKED,
    DONE,
    FAILED,
    JobFn,
    JobProgress,
    JobQueue,
    JobQueueFull,
    JobState,
)

#: Form defaults, kept identical to `videomaker new`'s CLI options so the two
#: front ends cannot quietly create differently-shaped projects.
DEFAULT_TEMPLATE = "tech_explainer"
DEFAULT_VOICE = "af_heart"
DEFAULT_MINUTES = 2.0

#: Which pill colour a derived status gets. Anything not listed is neutral —
#: `new` and the mid-pipeline states are neither good news nor bad.
_STATUS_TONES: dict[Status, str] = {
    Status.RENDERED: "status-ok",
    Status.PREVIEW_READY: "status-waiting",
}

router = APIRouter()


@dataclass(frozen=True)
class ProjectRow:
    """One line of the list page. Built fresh per request; never cached."""

    id: str
    topic: str
    template: str
    status: Status
    created_at: datetime

    @property
    def status_label(self) -> str:
        return self.status.value.replace("_", " ")

    @property
    def status_tone(self) -> str:
        return _STATUS_TONES.get(self.status, "")


def project_rows(store: ProjectStore) -> list[ProjectRow]:
    """Every readable project, newest first, each with its status derived now.

    A directory that will not load is skipped rather than raised: the workspace is
    a plain folder the user is invited to poke at, and one half-written
    `project.json` must not take the whole list page down with it.
    """
    rows: list[ProjectRow] = []
    for project_id in store.list_ids():
        try:
            project = store.load(project_id)
        except (OSError, ValueError):
            continue
        rows.append(
            ProjectRow(
                id=project.id,
                topic=project.topic,
                template=project.template,
                status=derive_status(project, stage_cache_for(store, project_id)),
                created_at=project.created_at,
            )
        )
    rows.sort(key=lambda row: row.created_at, reverse=True)
    return rows


def _render_index(
    request: Request,
    *,
    error: str = "",
    form: dict[str, object] | None = None,
    status_code: int = 200,
):
    """Render the list page, optionally with a rejected form's values and error."""
    templates: Jinja2Templates = request.app.state.templates
    names = list_templates()
    rows = project_rows(request.app.state.store)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "rows": rows,
            # The first-run screen is the list page's empty state rather than a
            # separate destination: the create form is the thing it argues for,
            # and a redirect would put a wall between reading it and using it.
            # The same partial is served on its own at `/guide` afterwards.
            "first_run": not rows,
            **welcome_context(request),
            "templates_available": names,
            # Never raises, even with no `ml` extra and no model weights — see
            # `web.voices`. A create form that 500s on a fresh clone is worse
            # than one offering five voices.
            "voices": available_voices(request.app.state.settings),
            "error": error,
            "form": form
            or {
                "topic": "",
                "template": DEFAULT_TEMPLATE if DEFAULT_TEMPLATE in names else "",
                "minutes": DEFAULT_MINUTES,
                "voice": DEFAULT_VOICE,
            },
        },
        status_code=status_code,
    )


def run_job(settings: Settings, project_id: str, *, until: str | None = None) -> JobFn:
    """A job body that runs the pipeline as far as `until` (all of it when None).

    Deps are built *inside* the job, on the worker thread, so the request handler
    never touches a quota ledger or a response cache — and so a job that waits in
    the queue for a while picks up the project as it is when it finally starts.
    """

    def job(progress: JobProgress) -> None:
        deps = build_deps(settings, project_id)
        project = deps.store.load(project_id)
        run_pipeline(project, deps, until=until, on_stage=progress)

    return job


def script_job(settings: Settings, project_id: str) -> JobFn:
    """The creation path's job: write the script and stop, well short of any gate."""
    return run_job(settings, project_id, until="script")


def advance_job(settings: Settings, project_id: str) -> JobFn:
    """The dashboard's job: run until the next unapproved gate stops it.

    There is no `until` here and that is the whole design. `run_pipeline` already
    raises `GateBlocked` at the first gate whose approval is missing, and the
    worker turns that into the terminal `blocked` state carrying the gate name —
    so "advance to the next gate" needs no second copy of where the gates are.
    Computing a stop stage here would be a duplicate of `GATE_BEFORE` that could
    drift out of step with the runner's own idea of the state machine.
    """
    return run_job(settings, project_id, until=None)


def welcome_context(request: Request) -> dict[str, object]:
    """What the onboarding panel says, all of it read live rather than asserted.

    The headroom comes from the ledger the providers are policed against and the
    key rows from the `Settings` they resolve credentials from, so the screen
    cannot promise a free tier that has already been spent or keys that are not
    there. See `web.onboarding`.
    """
    return {
        "free_tier": free_tier_rows(),
        "keys": key_rows(request.app.state.settings),
    }


@router.get("/")
def index(request: Request):
    """The project list, plus the create form — and, when empty, the first run."""
    return _render_index(request)


@router.get("/voice-options")
def voice_options(request: Request, language: str = ""):
    """The voice field, narrowed to one language. htmx swaps it in place.

    The language `<select>` is a filter and this route is all it does: nothing
    here writes anything, and the value never reaches `POST /projects`, which
    derives the project's language from the chosen voice instead. That is the
    whole of "one choice, not two" — there is no second value to disagree with.

    An unknown or empty filter offers everything rather than nothing. A stale
    request, or a language that has left the catalogue since the page loaded,
    must not leave the form with an empty voice menu and no way back.
    """
    voices = available_voices(request.app.state.settings)
    if language and language in voices.language_labels:
        voices = replace(
            voices, groups=[g for g in voices.groups if g.language == language]
        )
    first = voices.ids[0] if voices.ids else DEFAULT_VOICE
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "_voice_field.html", {"voices": voices, "form": {"voice": first}}
    )


@router.get("/guide")
def guide(request: Request):
    """The onboarding screen on its own, for anyone who has projects already.

    One screen, read once, reachable from the nav — deliberately not a wizard
    (`docs/guidance-and-chat-design.md`). It is the same partial the empty list
    page shows, so there is one copy of what the tool promises.
    """
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(request, "welcome.html", welcome_context(request))


@router.post("/projects")
def create_project(
    request: Request,
    topic: str = Form(""),
    template: str = Form(DEFAULT_TEMPLATE),
    minutes: float = Form(DEFAULT_MINUTES, gt=0),
    voice: str = Form(DEFAULT_VOICE),
):
    """Create a project and enqueue its script run, then redirect to its page.

    Validation failures re-render the form with a message and a 422 — never a 500,
    and never a bare JSON error, because this endpoint is reached by a browser
    posting a real `<form>`.
    """
    topic = topic.strip()
    submitted = {"topic": topic, "template": template, "minutes": minutes, "voice": voice}
    if not topic:
        return _render_index(
            request, error="Give the video a topic.", form=submitted, status_code=422
        )
    if template not in list_templates():
        return _render_index(
            request,
            error=f"Unknown template {template!r}.",
            form=submitted,
            status_code=422,
        )
    # The menu cannot offer a voice whose language this stack was measured to get
    # wrong, but a hand-rolled POST can still name one. Refusing here — rather
    # than creating the project and letting `voice` and `align` produce a
    # confidently wrong video — is the server-side half of that gate. Note the
    # `language` form field is deliberately *not* read: it is a filter, and the
    # project's language comes from the voice in `ProjectStore.create`.
    refusal = refusal_for(voice.strip())
    if refusal:
        return _render_index(request, error=refusal, form=submitted, status_code=422)

    store: ProjectStore = request.app.state.store
    project = store.create(topic, template, target_minutes=minutes, voice=voice.strip())

    jobs: JobQueue = request.app.state.jobs
    try:
        jobs.submit(project.id, "run", script_job(request.app.state.settings, project.id))
    except JobQueueFull as full:
        # The project is on disk either way; only the run failed to start, and the
        # project's own page can start it once the queue drains.
        raise HTTPException(status_code=503, detail=str(full)) from full

    return RedirectResponse(url=f"/projects/{project.id}", status_code=303)


# ------------------------------------------------------------------- dashboard

#: A human name and a one-line "what this stage does" for each `STAGE_ORDER`
#: entry. Presentation only — the set of stages, and their order, still comes
#: from `STAGE_ORDER`, so a stage added in M3 shows up here as itself rather
#: than silently disappearing from the stepper.
STAGE_LABELS: dict[str, tuple[str, str]] = {
    "script": ("Script", "narration for every scene"),
    "voice": ("Voice", "a spoken take per scene"),
    "align": ("Align", "word timings from the takes"),
    "visuals": ("Visuals", "a still or clip per scene"),
    "captions": ("Captions", "subtitles from the alignment"),
    "assemble": ("Assemble", "scenes joined into one timeline"),
    "render": ("Render", "the final wide mp4"),
}

#: Gate name -> the human title of the screen that clears it.
GATE_TITLES: dict[str, str] = {
    "script": "Script review",
    "storyboard": "Storyboard review",
    "preview": "Preview review",
}

#: Which pill colour a job state gets. `blocked` is amber, not red: a run that
#: stopped for a human is the product working as designed.
_JOB_TONES: dict[str, str] = {
    DONE: "status-ok",
    FAILED: "status-failed",
    BLOCKED: "status-waiting",
}

#: Job states that will never change again on their own. **The polling partial
#: omits `hx-trigger` for exactly these**, which is what ends the poll loop.
TERMINAL_STATES: frozenset[str] = frozenset({DONE, FAILED, BLOCKED})


def _terminal(job: JobState | None) -> bool:
    """True when there is nothing left to watch, so the partial must stop polling."""
    return job is None or job.state in TERMINAL_STATES


@dataclass(frozen=True)
class StageStep:
    """One rung of the stepper. `current` is `stage_is_current`, nothing else."""

    number: int
    stage: str
    label: str
    note: str
    current: bool

    @property
    def marker(self) -> str:
        return "true" if self.current else "false"

    @property
    def state_label(self) -> str:
        return "done" if self.current else "pending"


@dataclass(frozen=True)
class GateRow:
    """One of the three review gates, as the dashboard shows it."""

    name: str
    title: str
    stage: str
    review: str
    approved_at: datetime | None
    #: Every stage before the gated one is current, so the human can act on it now.
    reachable: bool

    @property
    def approved(self) -> bool:
        return self.approved_at is not None

    @property
    def marker(self) -> str:
        return "true" if self.approved else "false"

    @property
    def state_label(self) -> str:
        if self.approved:
            return "approved"
        return "ready to review" if self.reachable else "pending"

    @property
    def tone(self) -> str:
        if self.approved:
            return "status-ok"
        return "status-waiting" if self.reachable else ""


def _load(request: Request, project_id: str) -> Project:
    """The project, or a 404 — never a 500 for a URL someone typed by hand."""
    store: ProjectStore = request.app.state.store
    try:
        return store.load(project_id)
    except (OSError, ValueError) as missing:
        raise HTTPException(status_code=404, detail=f"no such project: {project_id}") from missing


def stage_steps(project: Project, stage_cache: StageCache) -> list[StageStep]:
    """The seven rungs, marked by `stage_is_current` and by nothing else.

    Decision 3 in one function: the page never decides for itself that "the first
    three are done". It asks the runner about each stage independently, which is
    why a narration edited under a voiced project un-marks `voice` and `align`
    here at the same moment it does on the CLI.
    """
    steps: list[StageStep] = []
    for number, stage in enumerate(STAGE_ORDER, start=1):
        label, note = STAGE_LABELS.get(stage, (stage.replace("_", " ").title(), ""))
        steps.append(
            StageStep(
                number=number,
                stage=stage,
                label=label,
                note=note,
                current=stage_is_current(project, stage_cache, stage),
            )
        )
    return steps


def gate_rows(project: Project, steps: list[StageStep]) -> list[GateRow]:
    """The three gates in pipeline order, each approved or pending.

    A gate is `reachable` once every stage before the one it guards is current:
    that is exactly when a run would stop there, and so exactly when sending the
    human to the review screen is useful rather than premature.
    """
    current = {step.stage: step.current for step in steps}
    rows: list[GateRow] = []
    for stage, gate in GATE_BEFORE.items():
        upstream = STAGE_ORDER[: STAGE_ORDER.index(stage)]
        rows.append(
            GateRow(
                name=gate,
                title=GATE_TITLES.get(gate, gate.title()),
                stage=stage,
                review=GATE_REVIEW[gate],
                approved_at=getattr(project.approvals, gate),
                reachable=all(current[earlier] for earlier in upstream),
            )
        )
    return rows


def _advance_label(gates: list[GateRow], *, busy: bool) -> str:
    """What the primary button says, given where the next run would stop."""
    if busy:
        return "Working…"
    pending = [gate for gate in gates if not gate.approved]
    if not pending:
        return "Run to the final render"
    return f"Run to the {pending[0].name} gate"


def _job_context(request: Request, project_id: str) -> dict[str, object]:
    """Everything `_job.html` needs, including whether it may ask to be polled."""
    jobs: JobQueue = request.app.state.jobs
    job = jobs.state_for(project_id)
    return {
        "project_id": project_id,
        "job": job,
        # The one decision this whole partial exists to make.
        "poll": not _terminal(job),
        "job_tone": _JOB_TONES.get(job.state, "") if job is not None else "",
        "gate_titles": GATE_TITLES,
    }


@router.get("/projects/{project_id}")
def project_detail(request: Request, project_id: str):
    """The dashboard: derived status, the stage stepper, the gates, and the job."""
    project = _load(request, project_id)
    store: ProjectStore = request.app.state.store
    stage_cache = stage_cache_for(store, project_id)

    steps = stage_steps(project, stage_cache)
    gates = gate_rows(project, steps)
    status = derive_status(project, stage_cache)
    job_context = _job_context(request, project_id)
    # `poll` is true exactly when a job exists and has not finished — which is
    # also exactly when starting another one would be pointless.
    busy = bool(job_context["poll"])

    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "project.html",
        {
            **job_context,
            "project": project,
            "steps": steps,
            "gates": gates,
            "status": status,
            "status_label": status.value.replace("_", " "),
            "status_tone": _STATUS_TONES.get(status, ""),
            "busy": busy,
            "advance_label": _advance_label(gates, busy=busy),
            # Read from the same walk `derive_status` performs, so the panel and
            # the pipeline cannot describe the project differently.
            "next": next_step(
                project,
                stage_cache,
                busy=busy,
                running_stage=job.stage if (job := job_context["job"]) is not None else None,
            ),
        },
    )


@router.get("/projects/{project_id}/job")
def project_job(request: Request, project_id: str):
    """The polled fragment. Cheap on purpose: it is fetched every 1.5 s."""
    _load(request, project_id)
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(request, "_job.html", _job_context(request, project_id))


@router.post("/projects/{project_id}/advance")
def advance_project(request: Request, project_id: str):
    """Enqueue a run to the next gate and redirect back to the dashboard.

    Decision 2: nothing is run here. A second click while a job is in flight is
    harmless — `JobQueue.submit` is a no-op for a project that already has one —
    so the redirect is unconditional and the page reports whatever state it finds.
    """
    _load(request, project_id)
    jobs: JobQueue = request.app.state.jobs
    try:
        jobs.submit(project_id, "run", advance_job(request.app.state.settings, project_id))
    except JobQueueFull as full:
        raise HTTPException(status_code=503, detail=str(full)) from full
    return RedirectResponse(url=f"/projects/{project_id}", status_code=303)
