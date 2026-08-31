"""Gate 3: watching the 480p preview, approving it, and watching the final render.

**This module's reason to exist is the progress bar.** Everything else here is
the shape gates 1 and 2 already established — a page, an approve form that stamps
and enqueues, a 303, and a live region that stops asking to be polled once there
is nothing left to watch. What is new is that the last stage of the run is also,
by a wide margin, the longest: M1 measured 70 s of encode inside a 184 s run.
`on_stage` fires once per *stage*, so a bar driven by it alone sits frozen at six
sevenths for the entire part of the run the user actually sits and watches, and
then jumps to done. That is worse than no bar, because it looks like a hang.

So the encode reports on itself. `run_ffmpeg` already emits **output seconds** to
an `on_progress` callback, and `assemble` already knows how many output seconds
the finished video has (`scene_timeline`). The ratio of the two is a real
completion fraction, and `encode_reporter` maps it onto the slice of the bar the
render stage owns — from `ENCODE_FLOOR` (everything before it) up to 1.0.

**Why `run_render` is instrumented rather than changed.** `run_render` takes no
`on_progress`, and M2's global constraint is that the pipeline is unchanged:
`videomaker/pipeline/` is off limits to this milestone apart from the 480p
preview artefact. So `encode_progress` substitutes an instrumented `run_ffmpeg`
into `videomaker.pipeline.render` for the duration of the one call and restores it
in a `finally`. That is a monkey-patch, and it is written down here rather than
tucked away because exactly one thing keeps it honest: **there is one worker
thread running one job at a time** (design decision 1) and handlers never run
stages (decision 2), so nothing else can be inside `run_render` while the
substitution stands. If M3 ever grows a second worker this has to become a real
parameter on `run_render` — that is the moment to spend the pipeline change.

The preview needs no such trick: `build_preview` takes `on_progress` directly, so
one reporter drives both bars.

**Staleness is judged by mtime here, not by re-hashing.** `build_preview` decides
for real, by content hash, and skips the encode when nothing moved — it is the
authority and it is cheap when the answer is "nothing to do". This page only has
to label a button, and it is re-rendered every 1.5 s while a job runs; digesting a
hundred megabytes of `build/video_wide.mp4` on every poll to choose between two
words would be an absurd price. mtime errs in the safe direction: an input
rewritten with identical bytes reads as stale, which costs one no-op rebuild,
while a changed input can never read as fresh.

**The pages poll themselves rather than `/job`.** `_job.html` is the dashboard's
partial and reports only the job; these pages have to reveal a `<video>` element
the moment the file behind it exists, which a job fragment cannot do. So the live
region carries `hx-select` naming its own id: htmx re-fetches the page, lifts that
section out and swaps it. The rule that ends the loop is unchanged and is the
important half — `hx-trigger` is emitted **only** while a job is in flight, so a
finished render stops making requests instead of re-reading `project.json` forty
times a minute for as long as the tab is left open.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from videomaker.cache import STAGE_ORDER
from videomaker.config import Settings
from videomaker.models import Aspect, Project
from videomaker.pipeline import render as render_stage
from videomaker.pipeline.assemble import (
    SCENE_GAP_S,
    narration_relpath,
    scene_timeline,
    video_relpath,
)
from videomaker.pipeline.captions import caption_relpath
from videomaker.pipeline.render import RENDER_ASPECTS, output_relpath
from videomaker.preview import STAGE as PREVIEW_STAGE
from videomaker.preview import build_preview, preview_relpath
from videomaker.project import ProjectStore
from videomaker.runner import (
    GATE_BEFORE,
    build_deps,
    derive_status,
    run_pipeline,
    stage_cache_for,
    stage_is_current,
)

# The shared 404 helper, the status palette, the job palette, the gate titles and
# the terminal-job rule all belong to the dashboard module. Importing them keeps
# one definition of each: a second 404 helper would be a second chance to return
# a 500 for a URL somebody typed by hand, and a second copy of the terminal-state
# set would be a second chance for a page to poll forever.
from videomaker.web.guidance import next_step
from videomaker.web.routes.projects import (
    _JOB_TONES,
    _STATUS_TONES,
    GATE_TITLES,
    _load,
    _terminal,
)
from videomaker.web.worker import (
    JobFn,
    JobProgress,
    JobQueue,
    JobQueueFull,
    JobState,
    stage_progress,
)

router = APIRouter()

#: The gate this page clears, and the stage it guards (`render`).
GATE = "preview"
GATED_STAGE = next(stage for stage, gate in GATE_BEFORE.items() if gate == GATE)

#: M2 is wide only (global constraint); 9:16 is M3.
ASPECT = Aspect.WIDE

#: Where the bar stands when the final encode is about to begin: the fraction
#: `on_stage` reported for the stage before the gated one. Derived from
#: `STAGE_ORDER` rather than written down, so a stage added in M3 moves it
#: without anyone having to remember that it needed moving.
ENCODE_FLOOR: float = stage_progress(STAGE_ORDER[STAGE_ORDER.index(GATED_STAGE) - 1]) or 0.0

#: How much of the whole bar has to be gained before another update is published.
#: FFmpeg is asked for progress every 10 ms of wall time, which for a long encode
#: is thousands of callbacks; each one takes the worker's lock, and no human can
#: see a tenth of a percent. At most ~140 updates across the render's seventh of
#: the bar: enough that it visibly moves, few enough that the lock is barely
#: touched.
ENCODE_STEP = 0.001

#: Prefix of the message an encode update carries. It is what distinguishes a
#: reading that came from inside FFmpeg from `on_stage`'s coarse end-of-stage
#: tick, on the page and in the tests.
ENCODE_NOTE = "encoding"

#: The job kinds that rewrite each page's artefact, and so hide it while they run.
PREVIEW_KINDS: tuple[str, ...] = ("preview",)
OUTPUT_KINDS: tuple[str, ...] = ("run",)

#: How often the live region re-fetches itself while a job is in flight. Same
#: cadence as the dashboard's job partial, for one rhythm across the UI.
POLL_MS = 1500


# --------------------------------------------------------------- progress maths


def timeline_seconds(project: Project) -> float:
    """How long the finished video is, in output seconds.

    Straight from `assemble`'s own timeline, so the denominator of the progress
    fraction is the same number the encode is working towards rather than a
    second estimate that could disagree with it.

    Summed over **every** aspect in `RENDER_ASPECTS`, because `run_render` encodes
    one file per aspect and the bar covers the whole stage. Denominating on wide
    alone made it sweep 0->1 once per aspect (M3 Task 6 finding).
    """
    return sum(
        segment.duration_s
        for aspect in RENDER_ASPECTS
        for segment in scene_timeline(project, gap_s=SCENE_GAP_S, aspect=aspect)
    )


def encode_fraction(seconds: float, total: float) -> float:
    """`seconds` of output as a fraction of `total`, clamped to 0…1.

    Clamped at the top because FFmpeg can report an `out_time` a frame or two
    past the nominal duration, and at the bottom because a project with nothing
    assemblable in it has a timeline of zero and no fraction to speak of.
    """
    if total <= 0:
        return 0.0
    return min(1.0, max(0.0, seconds / total))


def encode_reporter(
    progress: JobProgress,
    total: float,
    *,
    stage: str,
    floor: float = ENCODE_FLOOR,
    offset: float = 0.0,
) -> Callable[[float], None]:
    """A `run_ffmpeg` `on_progress` hook that drives `JobState.progress`.

    Output seconds land on the slice of the bar between `floor` and 1.0, which is
    the slice the encoding stage owns. Updates below `ENCODE_STEP` of movement are
    dropped: the point is a bar a human can see moving, not a lock contended
    thousands of times.
    """
    span = 1.0 - floor
    last = -1.0

    def report(seconds: float) -> None:
        nonlocal last
        # `seconds` restarts at zero for each aspect's file; `offset` is what the
        # earlier aspects already contributed, so the bar climbs once overall.
        fraction = encode_fraction(offset + seconds, total)
        value = floor + span * fraction
        if fraction < 1.0 and value - last < ENCODE_STEP:
            return
        last = value
        progress.update(
            stage=stage,
            progress=value,
            message=f"{ENCODE_NOTE} {offset + seconds:.1f}s of {total:.1f}s",
        )

    return report


@contextmanager
def encode_progress(
    progress: JobProgress, total_seconds: Callable[[], float], *, stage: str
) -> Iterator[None]:
    """Give the render stage's FFmpeg call a progress hook, for this block only.

    See the module docstring for why this is a substitution rather than an
    argument: the pipeline is closed to M2, and one worker thread running one job
    at a time is what makes the window exclusive. An explicit `on_progress` passed
    by the caller always wins, so this can only ever add reporting where there was
    none.

    **`total_seconds` is called when the encode starts, not when the job does.**
    Everything between those two moments — a re-voiced scene, a narration the
    script stage regenerated — is precisely what decides how long the finished
    video is. Measuring the timeline up front and holding on to the number gives a
    bar that pins at 100% a fifth of the way through a run that grew, which is the
    same uselessness as a bar that never moves, only harder to notice.
    """
    original = render_stage.run_ffmpeg
    encoded = 0.0  # output seconds finished by aspects already rendered

    def instrumented(args: list[str], *, cwd: Path | None = None, on_progress=None):
        nonlocal encoded
        if on_progress is not None:
            return original(args, cwd=cwd, on_progress=on_progress)
        reached = 0.0

        def hook(seconds: float) -> None:
            nonlocal reached
            reached = max(reached, seconds)
            report(seconds)

        report = encode_reporter(progress, total_seconds(), stage=stage, offset=encoded)
        try:
            return original(args, cwd=cwd, on_progress=hook)
        finally:
            # `run_render` calls this once per aspect, each reporting output
            # seconds from zero. Banking what this one reached is what keeps the
            # bar climbing once overall instead of resetting per file.
            encoded += reached

    render_stage.run_ffmpeg = instrumented
    try:
        yield
    finally:
        render_stage.run_ffmpeg = original


# ------------------------------------------------------------------- job bodies


def preview_job(settings: Settings, project_id: str) -> JobFn:
    """Bring the project up to `assemble`, then encode the 480p proxy.

    Running the pipeline first is not belt and braces: the preview is a picture of
    the *assembled* timeline, so previewing a project whose narration moved since
    the last assemble would show the reviewer something that is not what would
    ship. `run_pipeline` skips everything already current, so the usual cost of
    this line is nothing, and a project that has not cleared gates 1 and 2 stops
    at the right one with the right message rather than failing here.
    """

    def job(progress: JobProgress) -> None:
        deps = build_deps(settings, project_id)
        run_pipeline(deps.store.load(project_id), deps, until="assemble", on_stage=progress)
        project = deps.store.load(project_id)
        build_preview(
            project,
            deps,
            on_progress=encode_reporter(
                progress, timeline_seconds(project), stage=PREVIEW_STAGE
            ),
        )

    return job


def render_job(settings: Settings, project_id: str) -> JobFn:
    """Run to the end, with the final encode reporting its own progress.

    No `until`: `run_pipeline` stops at the first unapproved gate on its own, so
    "run to the end" needs no second copy of where the gates are.
    """

    def job(progress: JobProgress) -> None:
        deps = build_deps(settings, project_id)
        project = deps.store.load(project_id)
        # `project` is the object `run_pipeline` mutates, so reading the timeline
        # off it *when the encode starts* is reading it after every stage that
        # could have changed its length has already run.
        with encode_progress(progress, lambda: timeline_seconds(project), stage=GATED_STAGE):
            run_pipeline(project, deps, on_stage=progress)

    return job


# ------------------------------------------------------------------ view models


@dataclass(frozen=True)
class GateView:
    """Gate 3 as this page shows it. Derived from `project.approvals`, never stored."""

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
class ArtefactView:
    """One playable file: whether it is there, whether it is current, and its URLs.

    `state` is what the templates and the tests both read — `ready`, `stale` or
    `missing` — so the three-way answer is decided once, here, rather than by a
    chain of `{% if %}` in two templates that could drift apart.
    """

    project_id: str
    relpath: str
    path: Path
    exists: bool
    fresh: bool

    @property
    def state(self) -> str:
        if not self.exists:
            return "missing"
        return "ready" if self.fresh else "stale"

    @property
    def url(self) -> str:
        return f"/media/{self.project_id}/{self.relpath}"

    @property
    def location(self) -> str:
        """The absolute path, for copying into a shell or a file manager."""
        return str(self.path)

    @property
    def download_name(self) -> str:
        """The filename a download should land under.

        Not the artefact's own name: every project renders to `final_wide.mp4`, so
        downloading three projects would give `final_wide.mp4`,
        `final_wide(1).mp4`, `final_wide(2).mp4` and no way to tell them apart.
        Prefixing the project id keeps them identifiable in a Downloads folder.
        """
        return f"{self.project_id}-{Path(self.relpath).name}"


def _newest_input(root: Path) -> float:
    """The mtime of the most recently written thing the preview is made from."""
    inputs = (
        root / video_relpath(ASPECT),
        root / narration_relpath(ASPECT),
        root / caption_relpath(ASPECT),
    )
    stamps = [path.stat().st_mtime_ns for path in inputs if path.is_file()]
    return max(stamps) if stamps else 0.0


def assembled(root: Path) -> bool:
    """True when `assemble` has left the two files a preview needs."""
    return (root / video_relpath(ASPECT)).is_file() and (
        root / narration_relpath(ASPECT)
    ).is_file()


def preview_view(store: ProjectStore, project: Project) -> ArtefactView:
    """The 480p proxy: present, and newer than everything it was made from?"""
    root = store.path_for(project.id)
    path = root / preview_relpath(ASPECT)
    exists = path.is_file()
    fresh = exists and path.stat().st_mtime_ns >= _newest_input(root)
    return ArtefactView(
        project_id=project.id,
        relpath=preview_relpath(ASPECT),
        path=path,
        exists=exists,
        fresh=fresh,
    )


def output_view(store: ProjectStore, project: Project) -> ArtefactView:
    """The deliverable. Freshness is `stage_is_current`, the same answer the CLI gives."""
    root = store.path_for(project.id)
    path = root / output_relpath(ASPECT)
    exists = path.is_file()
    stage_cache = stage_cache_for(store, project.id)
    return ArtefactView(
        project_id=project.id,
        relpath=output_relpath(ASPECT),
        path=path,
        exists=exists,
        fresh=exists and stage_is_current(project, stage_cache, GATED_STAGE),
    )


def writing(job: JobState | None, kinds: tuple[str, ...]) -> bool:
    """True while a job that rewrites this page's artefact is still in flight.

    FFmpeg writes its output in place, so from the instant the encode starts
    there is a file at `output/final_wide.mp4` that is not a video yet. Offering
    it — as a `<video>` that spins forever, and then as a "this file is out of
    date" warning about the very render that is producing it — is worse than
    offering nothing. `kinds` is what makes this specific rather than "any job":
    a render running does not make the 480p preview unplayable, and vice versa.
    """
    return not _terminal(job) and job is not None and job.kind in kinds


def _job_view(request: Request, project_id: str) -> dict[str, object]:
    """The job, its pill colour, and the one decision that ends the polling loop."""
    jobs: JobQueue = request.app.state.jobs
    job = jobs.state_for(project_id)
    return {
        "job": job,
        "poll": not _terminal(job),
        "job_tone": _JOB_TONES.get(job.state, "") if job is not None else "",
        "gate_titles": GATE_TITLES,
        "poll_ms": POLL_MS,
    }


def _page_context(request: Request, project: Project) -> dict[str, object]:
    """What both pages share: the project, its derived status, and the job."""
    store: ProjectStore = request.app.state.store
    stage_cache = stage_cache_for(store, project.id)
    status = derive_status(project, stage_cache)
    job_view = _job_view(request, project.id)
    job: JobState | None = job_view["job"]  # type: ignore[assignment]
    return {
        **job_view,
        # Derived from the same walk `derive_status` just performed, so the panel
        # and the pipeline cannot describe the project differently.
        "next": next_step(
            project,
            stage_cache,
            busy=bool(job_view["poll"]),
            running_stage=job.stage if job is not None else None,
        ),
        "project": project,
        "status": status,
        "status_label": status.value.replace("_", " "),
        "status_tone": _STATUS_TONES.get(status, ""),
        "gate": GateView(
            name=GATE,
            title=GATE_TITLES.get(GATE, GATE.title()),
            approved_at=getattr(project.approvals, GATE),
        ),
    }


# -------------------------------------------------------------- the two pages


@router.get("/projects/{project_id}/preview")
def preview_page(request: Request, project_id: str):
    """Gate 3: play the 480p proxy, rebuild it, or approve and render.

    A project that has not been assembled is an **empty state, not a 404**: this
    is where the button that fixes it lives, and a page that refuses to load
    would leave the reviewer with nowhere to press it. The artefact URL itself
    still 404s, cleanly, through the usual media guard.
    """
    project = _load(request, project_id)
    store: ProjectStore = request.app.state.store

    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "preview.html",
        {
            **_page_context(request, project),
            "preview": preview_view(store, project),
            "assembled": assembled(store.path_for(project_id)),
            "seconds": timeline_seconds(project),
            "writing": writing(
                request.app.state.jobs.state_for(project_id), PREVIEW_KINDS
            ),
        },
    )


@router.post("/projects/{project_id}/preview/build")
def build_preview_action(request: Request, project_id: str):
    """Enqueue the 480p encode and redirect back. Decision 2: nothing runs here.

    A second click while a job is in flight is harmless — `JobQueue.submit` is a
    no-op for a project that already has one — so the redirect is unconditional
    and the page reports whatever state it finds.
    """
    _load(request, project_id)
    jobs: JobQueue = request.app.state.jobs
    try:
        jobs.submit(project_id, "preview", preview_job(request.app.state.settings, project_id))
    except JobQueueFull as full:
        raise HTTPException(status_code=503, detail=str(full)) from full
    return RedirectResponse(url=f"/projects/{project_id}/preview", status_code=303)


@router.post("/projects/{project_id}/approve/preview")
def approve_preview(request: Request, project_id: str):
    """Stamp gate 3, enqueue the final render, and redirect to the render page.

    Idempotent by construction: an existing stamp is left exactly as it is, so a
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
        jobs.submit(project_id, "run", render_job(request.app.state.settings, project_id))
    except JobQueueFull as full:
        raise HTTPException(status_code=503, detail=str(full)) from full
    return RedirectResponse(url=f"/projects/{project_id}/render", status_code=303)


@router.get("/projects/{project_id}/render")
def render_page(request: Request, project_id: str):
    """The render's progress, and — once there is one — the finished file.

    Also the page a failed render lands on, which is why FFmpeg's stderr tail is
    rendered here in full rather than collapsed into "the render failed": that
    tail is usually the only line that says what to change.
    """
    project = _load(request, project_id)
    store: ProjectStore = request.app.state.store

    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "render.html",
        {
            **_page_context(request, project),
            "output": output_view(store, project),
            "preview": preview_view(store, project),
            "seconds": timeline_seconds(project),
            "writing": writing(
                request.app.state.jobs.state_for(project_id), OUTPUT_KINDS
            ),
        },
    )
