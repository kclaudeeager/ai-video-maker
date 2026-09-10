"""The one background worker: a bounded queue, a single daemon thread, and the
thread-safe `JobState` records the UI polls.

**Why a thread and not a request handler.** M1's pipeline is synchronous by design
and rendering saturates the CPU (M0/M1 measured 70 s of encode inside a 184 s run).
`run_pipeline` also takes a blocking `fcntl.flock` for its whole duration. Either
property alone would be enough to keep it out of an async request handler; together
they make it a rule: **handlers enqueue and redirect, they never run stages.**

**Why exactly one worker.** A second concurrent render makes both renders slower and
can exhaust RAM, so the scheduler is deliberately the smallest thing that works —
one `queue.Queue`, one thread, one job at a time.

**Why the lock is the point of this module.** `JobState` is written by the worker
thread and read by request handlers on the event loop. Nothing here may rely on the
GIL: "a dict write is atomic" says nothing about a *pair* of writes, and a handler
that observed `state="done"` before `finished_at` was set would render a page that is
merely wrong rather than crashing — the most expensive kind of bug to find later. So
every read and every write of `_states` happens under `_lock`, and `state_for` hands
back a `dataclasses.replace` copy so a caller can never see a record mutate under it,
nor mutate ours.

**Why a job that raises is a normal day.** `GateBlocked` is the product working: the
run stopped for a human. It maps to `blocked`, never to `failed`. And nothing a job
raises — not even `BaseException` — may kill the worker, because a dead worker means
every later job hangs in `queued` forever with no error anywhere on screen.
"""

import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from videomaker.cache import STAGE_ORDER
from videomaker.pipeline.base import StageResult
from videomaker.runner import GATE_REVIEW, GateBlocked, StageFailed

#: What M2 submits. `run` advances the pipeline; `preview` builds the 480p proxy
#: (Task 11); `revoice` and `research` redo one scene.
JOB_KINDS: tuple[str, ...] = ("run", "preview", "revoice", "research", "reading")

#: The queue is bounded so a client looping on a submit endpoint cannot grow it
#: without limit. With one job per project, filling it takes 32 distinct projects.
MAX_QUEUED_JOBS = 32

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
BLOCKED = "blocked"


class JobQueueFull(RuntimeError):
    """The queue is at `MAX_QUEUED_JOBS`. The caller should tell the user to wait."""


@dataclass
class JobState:
    """A snapshot of one project's job, as the polling page sees it.

    Callers only ever hold copies (see `JobQueue.state_for`), so mutating one of
    these is harmless — and pointless.
    """

    project_id: str
    kind: str
    state: str
    stage: str = ""
    progress: float = 0.0
    message: str = ""
    error: str = ""
    #: Set when `state == "blocked"`: which gate stopped the run, so the page can
    #: link straight to the right review screen.
    gate: str = ""
    started_at: float | None = None
    finished_at: float | None = None


#: What a job body is handed so it can report where it has got to. It is callable
#: with `run_pipeline`'s `on_stage` signature, so a job can pass it straight through.
JobFn = Callable[["JobProgress"], object]


class JobProgress:
    """The reporting handle a running job is given.

    It is deliberately a callable with `(stage, result)` so a job can write
    `run_pipeline(project, deps, on_stage=progress)` with nothing in between.
    Task 12 uses `update()` to refine render progress from `run_ffmpeg`.
    """

    def __init__(self, jobs: "JobQueue", project_id: str) -> None:
        self._jobs = jobs
        self._project_id = project_id

    def __call__(self, stage: str, result: StageResult | None = None) -> None:
        """`run_pipeline`'s `on_stage`: record the stage and a coarse fraction."""
        self.update(stage=stage, progress=stage_progress(stage))

    def update(
        self,
        *,
        stage: str | None = None,
        progress: float | None = None,
        message: str | None = None,
    ) -> None:
        """Publish a partial update for this job. Progress never goes backwards."""
        self._jobs._publish(self._project_id, stage=stage, progress=progress, message=message)


def stage_progress(stage: str) -> float | None:
    """How far through `STAGE_ORDER` finishing `stage` leaves us, or None if unknown.

    Coarse on purpose: every stage counts the same even though `render` dominates
    the wall clock. Task 12 overlays the real encode progress on top.
    """
    if stage not in STAGE_ORDER:
        return None
    return (STAGE_ORDER.index(stage) + 1) / len(STAGE_ORDER)


#: Pushed onto the queue by `stop()` to wake a worker blocked in `get()`.
_STOP = object()


class JobQueue:
    """One daemon worker draining a bounded FIFO, publishing thread-safe progress."""

    def __init__(self, *, maxsize: int = MAX_QUEUED_JOBS) -> None:
        self._queue: queue.Queue = queue.Queue(maxsize=maxsize)
        self._lock = threading.Lock()
        self._states: dict[str, JobState] = {}
        #: Project ids submitted and not yet finished — the one-job-per-project
        #: guard. Counted from `submit` (not from `get`) so there is no window in
        #: which a job is invisible to `is_busy`.
        self._pending: set[str] = set()
        #: Set exactly when `_pending` is empty. Lets callers (and tests) wait for
        #: the worker to settle instead of sleeping and hoping.
        self._idle = threading.Event()
        self._idle.set()
        self._thread: threading.Thread | None = None
        self._stopping = False

    # -------------------------------------------------------------- lifecycle

    def start(self) -> None:
        """Start the worker. Idempotent, so a re-entered lifespan cannot double it."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping = False
            # A daemon thread cannot wedge interpreter exit if a render outlives
            # the shutdown timeout below.
            self._thread = threading.Thread(target=self._run, name="videomaker-worker", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Ask the worker to finish and join it. Safe to call twice, or never started.

        Queued-but-unstarted jobs are dropped: shutdown must not be held hostage by
        a backlog. A job already running is not interrupted — the stages have no
        cancellation point — so `timeout` bounds how long we wait for it.
        """
        with self._lock:
            thread, self._thread = self._thread, None
            self._stopping = True
        if thread is None:
            return
        self._drain()
        try:
            self._queue.put_nowait(_STOP)
        except queue.Full:  # pragma: no cover - _drain just emptied it
            pass
        thread.join(timeout)

    def _drain(self) -> None:
        """Discard queued jobs and stop counting them as pending."""
        while True:
            try:
                job = self._queue.get_nowait()
            except queue.Empty:
                return
            if job is not _STOP:
                self._retire(job[0])

    # ---------------------------------------------------------------- submit

    def submit(self, project_id: str, kind: str, fn: JobFn) -> JobState:
        """Enqueue `fn` for `project_id`, or return the job already in flight.

        Returns the resulting `JobState` (a copy). The plan writes this as `-> None`;
        returning the state is what makes "a second submit is a no-op returning the
        existing state" observable to a route that has to redirect either way.
        """
        with self._lock:
            if project_id in self._pending:
                # One job per project: a double-clicked button must not queue twice.
                return replace(self._states[project_id])
            state = JobState(project_id=project_id, kind=kind, state=QUEUED)
            try:
                self._queue.put_nowait((project_id, fn))
            except queue.Full:
                raise JobQueueFull(
                    f"the job queue is full ({self._queue.maxsize} waiting); try again shortly"
                ) from None
            # Only now is the job real: nothing above may leave `_pending` claiming
            # a job the queue rejected.
            self._states[project_id] = state
            self._pending.add(project_id)
            self._idle.clear()
            return replace(state)

    # ------------------------------------------------------------------ reads

    def state_for(self, project_id: str) -> JobState | None:
        """A **copy** of the project's latest job state, or None if it never had one."""
        with self._lock:
            state = self._states.get(project_id)
            return replace(state) if state is not None else None

    def states(self) -> dict[str, JobState]:
        """Copies of every known job state, keyed by project id."""
        with self._lock:
            return {pid: replace(state) for pid, state in self._states.items()}

    def is_busy(self) -> bool:
        """True while any job is queued or running — there is only ever one at a time."""
        with self._lock:
            return bool(self._pending)

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Block until nothing is queued or running. Returns False on timeout."""
        return self._idle.wait(timeout)

    # ----------------------------------------------------------------- worker

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            if job is _STOP:
                return
            project_id, fn = job
            with self._lock:
                if self._stopping:
                    return
            self._execute(project_id, fn)

    def _execute(self, project_id: str, fn: JobFn) -> None:
        """Run one job. This must not raise: a dead worker strands every later job."""
        self._publish(project_id, state=RUNNING, started_at=time.time())
        try:
            fn(JobProgress(self, project_id))
        except GateBlocked as blocked:
            # Not a failure. The pipeline is *supposed* to stop and ask a human.
            self._publish(
                project_id,
                state=BLOCKED,
                message=GATE_REVIEW[blocked.gate],
                gate=blocked.gate,
                finished_at=time.time(),
            )
        except StageFailed as failed:
            self._publish(
                project_id,
                state=FAILED,
                stage=failed.stage,
                error=str(failed.cause),
                finished_at=time.time(),
            )
        except BaseException as exc:  # noqa: BLE001 - a job body is arbitrary code
            self._publish(
                project_id,
                state=FAILED,
                error=str(exc) or exc.__class__.__name__,
                finished_at=time.time(),
            )
        else:
            self._publish(project_id, state=DONE, progress=1.0, finished_at=time.time())
        finally:
            self._retire(project_id)

    def _retire(self, project_id: str) -> None:
        """Drop the project's claim on the worker and signal idle when nothing is left."""
        with self._lock:
            self._pending.discard(project_id)
            if not self._pending:
                self._idle.set()

    def _publish(
        self,
        project_id: str,
        *,
        state: str | None = None,
        stage: str | None = None,
        progress: float | None = None,
        message: str | None = None,
        error: str | None = None,
        gate: str | None = None,
        started_at: float | None = None,
        finished_at: float | None = None,
    ) -> None:
        """Replace the stored record in one atomic step, under the lock.

        Every field lands together: a reader either sees the old record or the whole
        new one, never a half-applied update. Progress is clamped to be monotonic —
        a bar that jumps backwards reads as a bug even when the pipeline is fine.
        """
        with self._lock:
            current = self._states.get(project_id)
            if current is None:  # pragma: no cover - retired before a late report
                return
            if progress is not None:
                progress = max(current.progress, min(1.0, max(0.0, progress)))
            self._states[project_id] = replace(
                current,
                state=current.state if state is None else state,
                stage=current.stage if stage is None else stage,
                progress=current.progress if progress is None else progress,
                message=current.message if message is None else message,
                error=current.error if error is None else error,
                gate=current.gate if gate is None else gate,
                started_at=current.started_at if started_at is None else started_at,
                finished_at=current.finished_at if finished_at is None else finished_at,
            )
