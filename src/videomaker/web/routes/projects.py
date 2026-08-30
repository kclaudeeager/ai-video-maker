"""The project list (`GET /`) and project creation (`POST /projects`).

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
"""

from dataclasses import dataclass
from datetime import datetime

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from videomaker.config import Settings
from videomaker.models import Status
from videomaker.project import ProjectStore
from videomaker.runner import build_deps, derive_status, run_pipeline, stage_cache_for
from videomaker.templates import list_templates
from videomaker.web.worker import JobFn, JobProgress, JobQueue, JobQueueFull

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
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "rows": project_rows(request.app.state.store),
            "templates_available": names,
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


def script_job(settings: Settings, project_id: str) -> JobFn:
    """A job body that runs the pipeline as far as `script` and no further.

    Deps are built *inside* the job, on the worker thread, so the request handler
    never touches a quota ledger or a response cache — and so a job that waits in
    the queue for a while picks up the project as it is when it finally starts.
    """

    def job(progress: JobProgress) -> None:
        deps = build_deps(settings, project_id)
        project = deps.store.load(project_id)
        run_pipeline(project, deps, until="script", on_stage=progress)

    return job


@router.get("/")
def index(request: Request):
    """The project list, plus the create form."""
    return _render_index(request)


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
