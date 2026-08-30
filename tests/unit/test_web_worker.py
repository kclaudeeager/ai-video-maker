"""The single background worker: sequencing, exception translation, thread safety.

Every test here synchronises with `threading.Event`, never by sleeping and hoping.
A sleep-based test of a scheduler is a test that passes on a quiet laptop and fails
on a loaded CI runner, and the failure looks like a product bug rather than a test
bug — which is exactly the kind of cost this module exists to avoid.

`WAIT` is a deadlock guard, not a timing assumption: every `wait()` here is woken
by another thread's `set()`, so the timeout only fires when the code under test is
genuinely broken (and then we assert on it, so the failure is legible).
"""

import threading

import pytest

from videomaker.models import Status
from videomaker.pipeline.base import StageResult
from videomaker.runner import GATE_REVIEW, GateBlocked, StageFailed
from videomaker.web.worker import JobQueue, JobQueueFull, JobState

#: Seconds any `Event.wait()` may block before we call it a hang.
WAIT = 5.0


def waited(event: threading.Event, what: str = "event") -> None:
    """Block until `event` fires, failing loudly instead of hanging the suite."""
    assert event.wait(WAIT), f"timed out waiting for {what}"


@pytest.fixture
def jobs():
    """A started queue that is always stopped, even when the test fails."""
    queue = JobQueue()
    queue.start()
    try:
        yield queue
    finally:
        queue.stop()


# ------------------------------------------------------------------ happy path


def test_a_submitted_job_runs_and_reaches_done(jobs):
    ran = threading.Event()
    jobs.submit("demo", "run", lambda progress: ran.set())
    waited(ran, "the job to run")
    finished = _settled(jobs, "demo")
    assert finished.state == "done"
    assert finished.kind == "run"
    assert finished.progress == 1.0
    assert finished.started_at is not None
    assert finished.finished_at is not None
    assert finished.finished_at >= finished.started_at
    assert finished.error == ""


def test_state_for_an_unknown_project_is_none(jobs):
    assert jobs.state_for("never-submitted") is None


def test_submit_returns_a_queued_state(jobs):
    state = jobs.submit("demo", "run", lambda progress: None)
    assert isinstance(state, JobState)
    assert state.project_id == "demo"
    assert state.state in {"queued", "running", "done"}


# ------------------------------------------------------- exception translation


def test_a_raising_job_fails_and_the_worker_survives(jobs):
    def boom(progress):
        raise ValueError("kaboom")

    jobs.submit("bad", "run", boom)
    failed = _settled(jobs, "bad")
    assert failed.state == "failed"
    assert "kaboom" in failed.error

    # The whole point: the next job must still run on the same thread.
    ran = threading.Event()
    jobs.submit("good", "run", lambda progress: ran.set())
    waited(ran, "the job after a failure")
    assert _settled(jobs, "good").state == "done"


def test_gate_blocked_is_blocked_not_failed(jobs):
    def gated(progress):
        raise GateBlocked("storyboard", Status.VOICED)

    jobs.submit("gated", "run", gated)
    state = _settled(jobs, "gated")
    assert state.state == "blocked"
    assert state.message == GATE_REVIEW["storyboard"]
    assert state.gate == "storyboard"
    # A gate is a normal outcome, so nothing is reported as an error.
    assert state.error == ""


def test_stage_failed_records_the_stage_and_the_cause(jobs):
    def broken(progress):
        raise StageFailed("voice", RuntimeError("tts died"))

    jobs.submit("broken", "run", broken)
    state = _settled(jobs, "broken")
    assert state.state == "failed"
    assert state.stage == "voice"
    assert "tts died" in state.error


def test_a_baseexception_in_a_job_still_leaves_the_worker_alive(jobs):
    def rude(progress):
        raise KeyboardInterrupt

    jobs.submit("rude", "run", rude)
    assert _settled(jobs, "rude").state == "failed"
    ran = threading.Event()
    jobs.submit("after", "run", lambda progress: ran.set())
    waited(ran, "the job after a BaseException")


# ------------------------------------------------------- one job per project


def test_a_second_submit_for_a_busy_project_does_not_enqueue_twice(jobs):
    started, release = threading.Event(), threading.Event()
    calls: list[int] = []

    def blocking(progress):
        calls.append(1)
        started.set()
        waited(release, "release")

    first = jobs.submit("demo", "run", blocking)
    waited(started, "the first job to start")

    second = jobs.submit("demo", "preview", blocking)
    assert second.state == "running"
    assert second.kind == first.kind == "run"  # the *existing* job, not the new one

    release.set()
    assert _settled(jobs, "demo").state == "done"
    assert calls == [1], "the duplicate submit ran a second job"


def test_a_queued_project_is_not_enqueued_again(jobs):
    started, release = threading.Event(), threading.Event()

    def blocking(progress):
        started.set()
        waited(release, "release")

    jobs.submit("hog", "run", blocking)
    waited(started, "the hogging job to start")

    ran_twice: list[str] = []
    jobs.submit("demo", "run", lambda progress: ran_twice.append("a"))
    queued = jobs.submit("demo", "run", lambda progress: ran_twice.append("b"))
    assert queued.state == "queued"

    release.set()
    assert _settled(jobs, "demo").state == "done"
    assert ran_twice == ["a"]


def test_a_finished_project_can_be_submitted_again(jobs):
    first = threading.Event()
    jobs.submit("demo", "run", lambda progress: first.set())
    waited(first, "the first job")
    assert _settled(jobs, "demo").state == "done"

    second = threading.Event()
    jobs.submit("demo", "preview", lambda progress: second.set())
    waited(second, "the second job")
    assert _settled(jobs, "demo").kind == "preview"


def test_is_busy_tracks_the_worker(jobs):
    started, release = threading.Event(), threading.Event()

    def blocking(progress):
        started.set()
        waited(release, "release")

    assert not jobs.is_busy()
    jobs.submit("demo", "run", blocking)
    waited(started, "the job to start")
    assert jobs.is_busy()
    release.set()
    _settled(jobs, "demo")
    assert not jobs.is_busy()


# ------------------------------------------------------------------ sequencing


def test_two_projects_run_sequentially_and_never_concurrently(jobs):
    guard = threading.Lock()
    live = 0
    peak = 0
    started = {"a": threading.Event(), "b": threading.Event()}
    release = {"a": threading.Event(), "b": threading.Event()}

    def job(name: str):
        def run(progress):
            nonlocal live, peak
            with guard:
                live += 1
                peak = max(peak, live)
            started[name].set()
            waited(release[name], f"release {name}")
            with guard:
                live -= 1

        return run

    jobs.submit("a", "run", job("a"))
    jobs.submit("b", "run", job("b"))

    waited(started["a"], "job a to start")
    # b cannot have started: there is exactly one worker thread.
    assert not started["b"].is_set()
    assert jobs.state_for("b").state == "queued"

    release["a"].set()
    waited(started["b"], "job b to start")
    release["b"].set()

    assert _settled(jobs, "a").state == "done"
    assert _settled(jobs, "b").state == "done"
    with guard:
        assert peak == 1, f"{peak} jobs ran at once; the worker is not serialising"


# ------------------------------------------------------------------ boundedness


def test_a_full_queue_raises_job_queue_full():
    queue = JobQueue(maxsize=2)
    queue.start()
    started, release = threading.Event(), threading.Event()

    def blocking(progress):
        started.set()
        waited(release, "release")

    try:
        queue.submit("hog", "run", blocking)
        # Once the worker has picked the job up the queue is empty again, so the
        # next two submits are exactly what fills it.
        waited(started, "the hogging job to start")
        queue.submit("p1", "run", lambda progress: None)
        queue.submit("p2", "run", lambda progress: None)
        with pytest.raises(JobQueueFull):
            queue.submit("p3", "run", lambda progress: None)
        assert queue.state_for("p3") is None
    finally:
        release.set()
        queue.stop()


# ------------------------------------------------------------------ progress


def test_on_stage_updates_the_stage_and_advances_progress_monotonically(jobs):
    seen: list[float] = []

    def run(progress):
        progress("script", StageResult(changed=True))
        seen.append(jobs.state_for("demo").progress)
        progress("assemble", StageResult(changed=True))
        seen.append(jobs.state_for("demo").progress)
        # A late callback for an earlier stage must never wind the bar backwards.
        progress("script", StageResult(changed=False))
        seen.append(jobs.state_for("demo").progress)

    jobs.submit("demo", "run", run)
    assert _settled(jobs, "demo").state == "done"
    assert 0.0 < seen[0] < seen[1] <= 1.0
    assert seen[2] == seen[1]
    assert seen == sorted(seen)


def test_progress_reports_the_running_stage(jobs):
    reported, release = threading.Event(), threading.Event()

    def run(progress):
        progress("visuals", StageResult())
        reported.set()
        waited(release, "release")

    jobs.submit("demo", "run", run)
    waited(reported, "the stage report")
    assert jobs.state_for("demo").stage == "visuals"
    assert jobs.state_for("demo").state == "running"
    release.set()
    _settled(jobs, "demo")


# ------------------------------------------------------------------ isolation


def test_state_for_hands_out_a_copy(jobs):
    reported, release = threading.Event(), threading.Event()

    def run(progress):
        progress("script", StageResult())
        reported.set()
        waited(release, "release")

    jobs.submit("demo", "run", run)
    waited(reported, "the stage report")

    first = jobs.state_for("demo")
    second = jobs.state_for("demo")
    assert first is not second, "state_for handed out the live record"

    first.stage = "tampered"
    first.progress = 0.99
    assert jobs.state_for("demo").stage == "script"

    release.set()
    _settled(jobs, "demo")


# ------------------------------------------------------------------ lifecycle


def test_stop_joins_the_worker_thread():
    queue = JobQueue()
    queue.start()
    thread = queue._thread
    assert thread is not None and thread.is_alive()
    ran = threading.Event()
    queue.submit("demo", "run", lambda progress: ran.set())
    waited(ran, "the job to run")

    queue.stop()
    assert not thread.is_alive(), "stop() returned without joining the worker"
    assert queue._thread is None


def test_start_is_idempotent_and_stop_is_safe_twice():
    queue = JobQueue()
    queue.start()
    thread = queue._thread
    queue.start()
    assert queue._thread is thread
    queue.stop()
    queue.stop()
    assert not thread.is_alive()


def test_stop_without_start_is_a_no_op():
    JobQueue().stop()


def test_the_worker_thread_is_a_daemon():
    queue = JobQueue()
    queue.start()
    try:
        assert queue._thread.daemon, "a non-daemon worker would wedge interpreter exit"
    finally:
        queue.stop()


# ------------------------------------------------------------------ helpers


def _settled(queue: JobQueue, project_id: str) -> JobState:
    """The project's state once the worker has finished with it.

    Blocks on the queue's own idle signal rather than polling, so this never
    races the final `done`/`failed` write.
    """
    assert queue.wait_idle(WAIT), f"the worker never went idle for {project_id!r}"
    state = queue.state_for(project_id)
    assert state is not None
    return state


# ------------------------------------------------------------- app lifespan


def test_create_app_exposes_an_unstarted_queue(tmp_path):
    from videomaker.config import Settings
    from videomaker.web.app import create_app

    app = create_app(Settings(workspace_dir=tmp_path))
    assert isinstance(app.state.jobs, JobQueue)
    # TestClient without the context manager never runs lifespan; a queue that
    # started itself in the factory would leak a thread out of every such test.
    assert app.state.jobs._thread is None


def test_the_lifespan_starts_and_stops_the_worker(tmp_path):
    from fastapi.testclient import TestClient

    from videomaker.config import Settings
    from videomaker.web.app import create_app

    app = create_app(Settings(workspace_dir=tmp_path))
    before = threading.active_count()
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        thread = app.state.jobs._thread
        assert thread is not None and thread.is_alive()
        ran = threading.Event()
        app.state.jobs.submit("demo", "run", lambda progress: ran.set())
        waited(ran, "the job to run under the lifespan")
    assert not thread.is_alive()
    assert threading.active_count() == before, "the lifespan leaked a thread"
