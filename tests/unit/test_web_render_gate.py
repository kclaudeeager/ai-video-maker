"""Gate 3: the preview page, the approve action, and the render progress page.

**The test that earns this file's keep is
`test_progress_moves_while_the_encode_runs_not_only_between_stages`.**
A progress bar that only ticks when a stage finishes spends the whole render —
the longest part of the run, and the only part the user actually sits and watches
— frozen at six sevenths. So the encode's own `out_time` is wired through
`run_ffmpeg`'s `on_progress` into `JobState.progress` as a fraction of the
timeline `assemble` already knows the length of, and this file proves it two ways:

* deterministically, with a stubbed `run_ffmpeg` that reports known output
  seconds, so the arithmetic (`encode_fraction` and where it lands between the
  assemble floor and 1.0) is pinned exactly; and
* over **real FFmpeg**, so the wiring is proved against the thing that actually
  emits `out_time_ms` rather than against a stub's idea of it.

The other properties pinned here:

* **A preview nobody has built yet is an empty state, not a stack trace.** The
  page renders, says why there is nothing to play, and offers the button that
  fixes it; the artefact URL itself 404s cleanly rather than 500ing.
* **The handlers never run a stage** (design decision 2). Building a preview and
  approving the gate both enqueue, and the tests that assert it run with no
  lifespan — so there is no worker for an inline `run_pipeline` to hide behind.
* **Approving is idempotent**: a double-clicked button cannot rewrite the moment
  the human actually approved.
* **A failed render surfaces FFmpeg's stderr tail on the page.** The single most
  common real failure of this milestone is an encode that will not start, and a
  bare 500 hides the one line that says why.

Everything is on the mock provider chain: no network, no `ml` extra.
"""

import json
import re
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from videomaker import runner as runner_module
from videomaker.audio import MUSIC_KIND
from videomaker.config import Settings
from videomaker.media.ffmpeg import FFmpegError
from videomaker.models import Aspect, MusicSelection
from videomaker.pipeline import render as render_module
from videomaker.pipeline.assemble import (
    narration_relpath,
    scene_timeline,
    video_relpath,
)
from videomaker.pipeline.captions import caption_relpath
from videomaker.pipeline.render import RENDER_ASPECTS, music_bed, output_relpath
from videomaker.preview import PREVIEW_ASPECTS, build_preview, preview_relpath
from videomaker.project import ProjectStore
from videomaker.runner import build_deps, run_pipeline, stage_cache_for, stage_is_current
from videomaker.web.app import create_app
from videomaker.web.routes.render import (
    ENCODE_FLOOR,
    MUSIC_README,
    encode_fraction,
    encode_progress,
    music_selection,
    timeline_seconds,
)
from videomaker.web.worker import JobProgress

#: The same blunt no-CDN guard the other page tests use.
_ABSOLUTE_URL = re.compile(r"https?://", re.IGNORECASE)

#: Same `data-gate`/`data-approved` contract the dashboard and gates 1 and 2 use,
#: so no two pages can describe one approval differently.
_GATE = re.compile(r'data-gate="([a-z]+)" data-approved="(true|false)"')

#: Two words a scene, so the real encodes in this file stay to a few seconds.
SHORT_NARRATION = "Scene {number}."

#: What the encode reporter prefixes its message with. Recorded updates carrying
#: it are the ones that came from inside FFmpeg, as opposed to `on_stage`'s
#: coarse between-stages tick — which is the whole distinction under test.
ENCODE_NOTE = "encoding"


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    # The suite-wide fixture already redirects this; pinning it per test as well
    # keeps the response cache and quota ledger inside *this* test's tmp dir.
    monkeypatch.setattr(runner_module, "USER_CACHE_DIR", tmp_path / "cache")
    return Settings(workspace_dir=tmp_path / "workspace")


@pytest.fixture
def app(settings):
    return create_app(settings, providers="mock")


@pytest.fixture
def client(app) -> TestClient:
    """A client that does **not** enter the lifespan, so no worker thread runs.

    That is what makes "the handler enqueues rather than runs" observable: a job
    submitted here stays `queued` forever.
    """
    return TestClient(app)


@pytest.fixture
def store(app) -> ProjectStore:
    return app.state.store


@pytest.fixture
def locks_taken(monkeypatch) -> list[str]:
    """Every `ProjectStore.lock` the code under test opens.

    A page view must not appear here at all: `run_pipeline` holds this same
    `flock` for the length of a whole render, so a GET that took it would park a
    request thread for minutes while the render it is reporting on runs.
    """
    taken: list[str] = []
    original = ProjectStore.lock

    def spying_lock(self: ProjectStore, project_id: str):
        taken.append(project_id)
        return original(self, project_id)

    monkeypatch.setattr(ProjectStore, "lock", spying_lock)
    return taken


@pytest.fixture
def progress_updates(monkeypatch) -> list[tuple[str | None, float | None, str | None]]:
    """Every `JobProgress.update`, so the bar's movement can be read after the fact.

    Sampling `JobQueue.state_for` from the test thread would only ever catch
    whatever the worker happened to be on at that instant; recording the updates
    themselves is the only way to say *how* the bar moved rather than where it
    finished.
    """
    seen: list[tuple[str | None, float | None, str | None]] = []
    original = JobProgress.update

    def recording(self, *, stage=None, progress=None, message=None) -> None:
        seen.append((stage, progress, message))
        original(self, stage=stage, progress=progress, message=message)

    monkeypatch.setattr(JobProgress, "update", recording)
    return seen


def _new(store: ProjectStore, topic: str = "how ssds work"):
    return store.create(topic, "tech_explainer", target_minutes=0.5)


def _scripted(app, store: ProjectStore):
    """A project with a script, shortened so the later real encodes stay quick."""
    project = _new(store)
    run_pipeline(project, build_deps(app.state.settings, project.id), until="script", yes=True)
    project = store.load(project.id)
    for number, scene in enumerate(project.scenes, start=1):
        scene.narration = SHORT_NARRATION.format(number=number)
    store.save(project)
    return store.load(project.id)


def _assembled(app, store: ProjectStore):
    """A project run as far as `assemble` — exactly where gate 3 finds one.

    `yes=True` stamps gates 1 and 2 on the way past. Gate 3 sits before `render`,
    which `until="assemble"` never reaches, so the preview approval is still
    unstamped — which is the state this whole page exists to clear.
    """
    project = _scripted(app, store)
    run_pipeline(
        project, build_deps(app.state.settings, project.id), until="assemble", yes=True
    )
    return store.load(project.id)


def _previewed(app, store: ProjectStore):
    """An assembled project whose 480p preview has already been encoded."""
    project = _assembled(app, store)
    deps = build_deps(app.state.settings, project.id)
    build_preview(deps.store.load(project.id), deps)
    return store.load(project.id)


def _gates(body: str) -> dict[str, str]:
    return dict(_GATE.findall(body))


# ----------------------------------------------------------- the preview page


def test_the_preview_page_of_an_unknown_project_is_a_404(client):
    assert client.get("/projects/no-such-project/preview").status_code == 404


def test_the_render_page_of_an_unknown_project_is_a_404(client):
    assert client.get("/projects/no-such-project/render").status_code == 404


def test_an_unassembled_project_gets_an_empty_state_and_the_artefact_404s(app, client, store):
    """Nothing to play yet is a page that says so, never a 500 and never a stack trace."""
    project = _scripted(app, store)

    response = client.get(f"/projects/{project.id}/preview")

    assert response.status_code == 200
    assert 'data-preview="missing"' in response.text
    assert "<video" not in response.text
    # The artefact the page would have played, asked for directly.
    missing = client.get(f"/media/{project.id}/{preview_relpath(Aspect.WIDE)}")
    assert missing.status_code == 404


def test_the_preview_page_plays_the_built_preview(app, client, store):
    project = _previewed(app, store)

    body = client.get(f"/projects/{project.id}/preview").text

    assert 'data-preview="ready"' in body
    assert "<video" in body
    assert f"/media/{project.id}/{preview_relpath(Aspect.WIDE)}" in body
    served = client.get(f"/media/{project.id}/{preview_relpath(Aspect.WIDE)}")
    assert served.status_code == 200
    assert served.content


def test_the_preview_page_offers_the_gate_and_loads_no_cdn(app, client, store):
    project = _previewed(app, store)

    body = client.get(f"/projects/{project.id}/preview").text

    assert _gates(body)["preview"] == "false"
    assert f"/projects/{project.id}/approve/preview" in body
    assert not _ABSOLUTE_URL.search(body)


def test_viewing_the_preview_page_takes_no_project_lock(app, client, store, locks_taken):
    project = _previewed(app, store)
    locks_taken.clear()

    client.get(f"/projects/{project.id}/preview")

    assert locks_taken == []


# ------------------------------------------------------- building the preview


def test_building_the_preview_is_enqueued_and_not_run_in_the_handler(app, client, store):
    project = _assembled(app, store)

    response = client.post(f"/projects/{project.id}/preview/build", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/projects/{project.id}/preview"
    job = app.state.jobs.state_for(project.id)
    assert job is not None
    assert (job.kind, job.state) == ("preview", "queued")
    # No worker is running, so a preview that exists could only have been built
    # inside the request handler.
    assert not (store.path_for(project.id) / preview_relpath(Aspect.WIDE)).is_file()


def test_the_build_job_really_encodes_the_preview(app, store):
    project = _assembled(app, store)

    with TestClient(app) as client:
        client.post(f"/projects/{project.id}/preview/build")
        assert app.state.jobs.wait_idle(240)
        job = app.state.jobs.state_for(project.id)
        assert job.state == "done", job.error
        body = client.get(f"/projects/{project.id}/preview").text

    assert (store.path_for(project.id) / preview_relpath(Aspect.WIDE)).is_file()
    assert 'data-preview="ready"' in body


# ---------------------------------------------------------------- approving


def test_approving_stamps_the_gate_and_redirects_to_the_render_page(app, client, store):
    project = _previewed(app, store)
    assert store.load(project.id).approvals.preview is None

    response = client.post(f"/projects/{project.id}/approve/preview", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/projects/{project.id}/render"
    assert store.load(project.id).approvals.preview is not None


def test_approving_enqueues_the_render_rather_than_running_it(app, client, store):
    project = _previewed(app, store)

    client.post(f"/projects/{project.id}/approve/preview", follow_redirects=False)

    job = app.state.jobs.state_for(project.id)
    assert job is not None
    assert job.state == "queued"
    # With no lifespan there is no worker, so an output file here would mean the
    # handler rendered inline (decision 2).
    assert not (store.path_for(project.id) / output_relpath(Aspect.WIDE)).is_file()


def test_approving_twice_keeps_the_first_stamp(app, client, store):
    project = _previewed(app, store)

    client.post(f"/projects/{project.id}/approve/preview", follow_redirects=False)
    first = store.load(project.id).approvals.preview
    client.post(f"/projects/{project.id}/approve/preview", follow_redirects=False)

    assert store.load(project.id).approvals.preview == first


# ------------------------------------------------------------ progress maths


def test_encode_fraction_is_output_seconds_over_the_timeline():
    assert encode_fraction(0.0, 40.0) == 0.0
    assert encode_fraction(10.0, 40.0) == pytest.approx(0.25)
    assert encode_fraction(40.0, 40.0) == 1.0
    # FFmpeg can overshoot the nominal duration by a frame or two, and a timeline
    # of zero is a project with nothing assemblable in it.
    assert encode_fraction(41.0, 40.0) == 1.0
    assert encode_fraction(1.0, 0.0) == 0.0


def test_the_timeline_length_is_what_assemble_built(app, store):
    project = _assembled(app, store)

    total = timeline_seconds(project)

    assert total > 0
    # Every aspect `run_render` encodes, not just wide: the bar covers the stage,
    # and the stage writes one file per aspect.
    assert total == pytest.approx(
        sum(
            segment.duration_s
            for aspect in RENDER_ASPECTS
            for segment in scene_timeline(project, gap_s=0.5, aspect=aspect)
        )
    )
    assert total > sum(
        segment.duration_s for segment in scene_timeline(project, gap_s=0.5, aspect=Aspect.WIDE)
    ), "a wide-only denominator is what made the bar sweep twice"


def test_the_timeline_is_measured_when_the_encode_starts_not_when_the_job_does(monkeypatch):
    """A run that changes the video's length must not leave the bar pinned at 100%.

    The stages between "job accepted" and "FFmpeg opened its output" are exactly
    the ones that decide how long the finished video is — a regenerated script, a
    re-voiced scene. A total measured up front and held on to gives a bar that
    reaches the end a fifth of the way through, which is no more use than one that
    never moves.
    """
    published: list[tuple[str | None, float | None, str | None]] = []

    class Recorder:
        def update(self, *, stage=None, progress=None, message=None) -> None:
            published.append((stage, progress, message))

    def stub_ffmpeg(args, *, cwd=None, on_progress=None):
        on_progress(20.0)

    monkeypatch.setattr(render_module, "run_ffmpeg", stub_ffmpeg)
    timeline = [10.0]

    with encode_progress(Recorder(), lambda: timeline[0], stage="render"):
        # The run grew the video after the job started but before the encode began.
        timeline[0] = 40.0
        render_module.run_ffmpeg(["-i", "in.mp4", "out.mp4"])

    assert published == [
        ("render", ENCODE_FLOOR + (1.0 - ENCODE_FLOOR) * 0.5, "encoding 20.0s of 40.0s")
    ]


def test_the_substitution_is_undone_even_when_the_encode_raises(monkeypatch):
    """A failed render must not leave an instrumented `run_ffmpeg` behind it."""

    def stub_ffmpeg(args, *, cwd=None, on_progress=None):
        raise FFmpegError("nope", returncode=1, stderr_tail="nope")

    monkeypatch.setattr(render_module, "run_ffmpeg", stub_ffmpeg)

    class Recorder:
        def update(self, **kwargs) -> None:
            pass

    with pytest.raises(FFmpegError), encode_progress(
        Recorder(), lambda: 1.0, stage="render"
    ):
        render_module.run_ffmpeg(["-i", "in.mp4", "out.mp4"])

    assert render_module.run_ffmpeg is stub_ffmpeg


def _reported_runs(values: list[float]) -> list[list[float]]:
    """The raw FFmpeg second-readings, split per aspect (each restarts at zero)."""
    runs: list[list[float]] = [[]]
    for value in values:
        if runs[-1] and value < runs[-1][-1]:
            runs.append([])
        runs[-1].append(value)
    return runs


def _ascending_runs(values: list[float]) -> list[list[float]]:
    """Split `values` at every point the bar goes backwards.

    `run_render` encodes one aspect after another, each reporting output seconds from
    zero. `encode_progress` banks what each aspect reached and offsets the next, and
    `timeline_seconds` denominates on every aspect in `RENDER_ASPECTS`, so the bar
    climbs **once** across the whole stage. Before that fix it rewound to the floor
    when the second file started — a bar that visibly restarts reads as a hang or a
    crash, which is worse than a coarse bar.
    """
    runs: list[list[float]] = [[]]
    for value in values:
        if runs[-1] and value < runs[-1][-1]:
            runs.append([])
        runs[-1].append(value)
    return runs


def test_progress_moves_while_the_encode_runs_not_only_between_stages(
    app, store, progress_updates
):
    """Real FFmpeg, real `out_time_ms`, and a bar that moves during the encode."""
    project = _previewed(app, store)

    with TestClient(app) as client:
        client.post(f"/projects/{project.id}/approve/preview")
        assert app.state.jobs.wait_idle(600)
        job = app.state.jobs.state_for(project.id)
        assert job.state == "done", job.error

    assert job.progress == 1.0

    encode = [
        value
        for stage, value, message in progress_updates
        if stage == "render" and value is not None and (message or "").startswith(ENCODE_NOTE)
    ]
    # Several distinct readings, all of them strictly inside the render stage's
    # own slice of the bar — that is exactly the movement `on_stage` alone cannot
    # produce, since it ticks once at the end of the stage and lands on 1.0.
    assert len(set(encode)) >= 3, encode
    runs = _ascending_runs(encode)
    assert len(runs) == 1, encode  # one climb for the whole stage, not one per aspect
    assert all(run == sorted(run) for run in runs), encode
    assert all(ENCODE_FLOOR <= value <= 1.0 for value in encode), encode
    assert any(ENCODE_FLOOR < value < 1.0 for value in encode), encode


def test_the_encode_fraction_is_mapped_onto_the_bar_exactly(app, store, monkeypatch,
                                                            progress_updates):
    """The same wiring, pinned arithmetically against a stub that reports known seconds."""
    project = _previewed(app, store)
    total = timeline_seconds(project)
    reported = [0.0, total / 4, total / 2, total]

    def stub_ffmpeg(args, *, cwd=None, on_progress=None):
        assert on_progress is not None, "the render's encode was not given a progress hook"
        for seconds in reported:
            on_progress(seconds)
        (cwd / args[-1]).write_bytes(b"not really an mp4")

    monkeypatch.setattr(render_module, "run_ffmpeg", stub_ffmpeg)

    with TestClient(app) as client:
        client.post(f"/projects/{project.id}/approve/preview")
        assert app.state.jobs.wait_idle(240)
        assert app.state.jobs.state_for(project.id).state == "done"

    encode = [
        value
        for stage, value, message in progress_updates
        if stage == "render" and value is not None and (message or "").startswith(ENCODE_NOTE)
    ]
    span = 1.0 - ENCODE_FLOOR
    # One continuous climb across both aspects: each file reports output seconds
    # from zero, and `encode_progress` offsets the second by what the first reached.
    banked = 0.0
    expected: list[float] = []
    for _aspect in RENDER_ASPECTS:  # the stub replays its script for each file
        expected += [
            ENCODE_FLOOR + span * encode_fraction(banked + seconds, total) for seconds in reported
        ]
        banked += max(reported)
    assert encode == pytest.approx(expected)
    assert encode == sorted(encode), "the bar must never rewind between aspects"


# --------------------------------------------------------------- render page


def test_the_render_page_shows_a_playable_file_and_its_path_when_done(app, store):
    project = _previewed(app, store)
    expected = store.path_for(project.id) / output_relpath(Aspect.WIDE)

    with TestClient(app) as client:
        client.post(f"/projects/{project.id}/approve/preview")
        assert app.state.jobs.wait_idle(600)
        job = app.state.jobs.state_for(project.id)
        assert job.state == "done", job.error
        body = client.get(f"/projects/{project.id}/render").text
        served = client.get(f"/media/{project.id}/{output_relpath(Aspect.WIDE)}")

    assert expected.is_file()
    assert 'data-render="ready"' in body
    assert "<video" in body
    assert f"/media/{project.id}/{output_relpath(Aspect.WIDE)}" in body
    # Shown verbatim so it can be copied straight into a shell or a file manager.
    assert str(expected) in body
    assert served.status_code == 200
    assert served.content
    assert not _ABSOLUTE_URL.search(body)


def test_the_render_page_polls_while_a_job_runs_and_stops_when_it_ends(app, client, store):
    project = _previewed(app, store)

    client.post(f"/projects/{project.id}/approve/preview", follow_redirects=False)
    running = client.get(f"/projects/{project.id}/render").text

    # Queued, with no worker to finish it: the page must ask to be polled.
    assert "hx-trigger" in running

    app.state.jobs._states[project.id].state = "done"
    finished = client.get(f"/projects/{project.id}/render").text

    assert "hx-trigger" not in finished


def test_a_half_written_output_is_not_offered_as_playable(app, client, store):
    """FFmpeg writes in place, so a file exists from the first frame to the last.

    Offering it mid-encode gives a `<video>` that spins forever *and* a "this
    file is out of date" warning about the very render producing it — the page
    contradicting itself at the one moment the user is watching it closely.
    """
    project = _previewed(app, store)
    output = store.path_for(project.id) / output_relpath(Aspect.WIDE)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"half an mp4")

    # No lifespan, so the job stays queued and the page sees a render in flight.
    client.post(f"/projects/{project.id}/approve/preview", follow_redirects=False)
    body = client.get(f"/projects/{project.id}/render").text

    assert "<video" not in body
    assert "out of date" not in body


def test_a_render_in_flight_does_not_hide_the_preview(app, client, store):
    """The two artefacts are independent: rendering does not touch the 480p proxy."""
    project = _previewed(app, store)

    client.post(f"/projects/{project.id}/approve/preview", follow_redirects=False)
    body = client.get(f"/projects/{project.id}/preview").text

    assert 'data-preview="ready"' in body
    assert "<video" in body


def test_a_failed_render_shows_the_ffmpeg_stderr_tail_rather_than_a_500(
    app, store, monkeypatch
):
    tail = "[libx264 @ 0x1] height not divisible by 2\nConversion failed!"

    def exploding_ffmpeg(args, *, cwd=None, on_progress=None):
        raise FFmpegError(
            f"ffmpeg exited with status 1:\n{tail}", returncode=1, stderr_tail=tail
        )

    project = _previewed(app, store)
    monkeypatch.setattr(render_module, "run_ffmpeg", exploding_ffmpeg)

    with TestClient(app) as client:
        client.post(f"/projects/{project.id}/approve/preview")
        assert app.state.jobs.wait_idle(240)
        job = app.state.jobs.state_for(project.id)
        response = client.get(f"/projects/{project.id}/render")

    assert job.state == "failed"
    assert job.stage == "render"
    assert response.status_code == 200
    assert "height not divisible by 2" in response.text
    assert "Conversion failed!" in response.text


def test_the_download_name_identifies_the_project():
    """Every project renders to `final_wide.mp4`, so the bare name is ambiguous.

    Downloading three projects would give `final_wide.mp4`, `final_wide(1).mp4`
    and `final_wide(2).mp4` with no way to tell them apart in a Downloads folder.
    """
    from videomaker.web.routes.render import ArtefactView

    view = ArtefactView(
        project_id="how-ssds-work",
        relpath=output_relpath(Aspect.WIDE),
        path=Path("/tmp/how-ssds-work") / output_relpath(Aspect.WIDE),
        exists=True,
        fresh=True,
    )
    assert view.download_name == "how-ssds-work-final_wide.mp4"
    assert view.url == "/media/how-ssds-work/output/final_wide.mp4"


# ======================================================================
# M3 Task 11: the music picker, and both cuts side by side
# ======================================================================
#
# Two properties carry this section.
#
# **A no-op write must not even reach for the `flock`.** `run_pipeline` holds
# that lock for the length of a whole render, so a picker that locked before
# comparing would park a request thread behind a twenty-minute encode every time
# someone re-submitted the choice already on the project. M2 Task 9's mutant 5
# caught exactly this, and `locks_taken == []` is the assertion that keeps it
# caught.
#
# **Changing the music invalidates the render and nothing else.** The audio
# layer's whole reach into the cache engine is `render_hash` (Task 9): the
# track's key, its levels and its bytes. Nothing upstream hashes
# `project.music`, so a new bed re-encodes `output/final_*.mp4` and re-assembles,
# re-captions and re-voices nothing. `test_changing_the_track_re_renders_and_
# restages_nothing_earlier` proves that over real FFmpeg, by mtime.
#
# The empty library is the default path, not an edge case: `assets/music/` is
# empty on every fresh clone, so the first test here is the one that runs for
# everybody.

#: Each panel's own marker. Adjacent, like every other `data-*` pair on the gates.
_CUT = re.compile(r'data-preview-aspect="([a-z]+)" data-preview-state="([a-z]+)"')
#: The picker's marker: is there a library at all, and how much is in it?
_LIBRARY = re.compile(r'data-music-library="([a-z]+)" data-music-tracks="(\d+)"')

CALM = f"{MUSIC_KIND}/calm/rain.wav"
UPBEAT = f"{MUSIC_KIND}/upbeat/pulse.wav"


def _cuts(body: str) -> dict[str, str]:
    return dict(_CUT.findall(body))


@pytest.fixture(scope="module")
def tone(tmp_path_factory):
    """One generated track. The project ships no audio, so neither may its tests."""
    path = tmp_path_factory.mktemp("gate_library") / "tone.wav"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostdin", "-y",
            "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=44100",
            "-t", "6", "-ac", "2", str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


@pytest.fixture
def library(app, tmp_path, tone) -> Path:
    """Two real tracks in two moods, with the app's settings pointed at them.

    `app.state.settings` rather than the `settings` fixture: `create_app` forces
    the provider chain, and `provider_override` returns a *new* `Settings`. The
    app's copy is the one every handler reads.
    """
    root = tmp_path / "library" / "music"
    for key in (CALM, UPBEAT):
        path = root / key.removeprefix(f"{MUSIC_KIND}/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(tone.read_bytes())
    app.state.settings.music_dir = root
    app.state.settings.sfx_dir = tmp_path / "library" / "sfx"
    return root


def _pick(**overrides) -> dict[str, str]:
    """A full picker submission — every field, as the one form really posts it."""
    return {
        "enabled": "on",
        "track_key": "",
        "volume_db": str(-18.0),
        "duck_db": str(12.0),
        "sfx": "on",
        **overrides,
    }


def _stamp(path: Path) -> int:
    return path.stat().st_mtime_ns if path.is_file() else 0


def _other_track(app, project_id: str) -> str:
    """A track key that really changes the bed, whatever the mood picked on its own.

    With a library present and `enabled` defaulted on, a project already *has* a
    bed before anybody touches the picker — `select_track` chose it from the
    template's mood. Naming that same track is not a change, and the cache is right
    to skip the encode, so a test that wants a change has to ask for the other one.
    """
    deps = build_deps(app.state.settings, project_id)
    bed = music_bed(deps.store.load(project_id), deps)
    return UPBEAT if bed is not None and bed.key == CALM else CALM


# ------------------------------------------------------------ the empty library


def test_an_empty_library_is_an_honest_empty_state_pointing_at_the_readme(
    app, client, store
):
    """Every fresh clone. Not an edge case — the default path through this page."""
    project = _previewed(app, store)

    body = client.get(f"/projects/{project.id}/preview").text

    assert _LIBRARY.search(body).groups() == ("empty", "0")
    assert MUSIC_README in body
    assert "<option" not in body.split('id="music-picker"')[1].split("</section>")[0]
    assert not _ABSOLUTE_URL.search(body)


def test_an_empty_library_still_renders_and_still_approves(app, client, store):
    """Absence of a library is never an error (docs/audio-design.md)."""
    project = _previewed(app, store)

    body = client.get(f"/projects/{project.id}/preview").text

    assert _gates(body)["preview"] == "false"
    assert f"/projects/{project.id}/approve/preview" in body


# ------------------------------------------------------------------ the picker


def test_the_picker_lists_every_track_in_the_library(app, client, store, library):
    project = _previewed(app, store)

    body = client.get(f"/projects/{project.id}/preview").text

    assert _LIBRARY.search(body).groups() == ("ready", "2")
    assert CALM in body
    assert UPBEAT in body
    assert f"/projects/{project.id}/music" in body


def test_choosing_a_track_writes_it_onto_the_project(app, client, store, library):
    project = _previewed(app, store)

    response = client.post(
        f"/projects/{project.id}/music", data=_pick(track_key=CALM), follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/projects/{project.id}/preview#music"
    assert store.load(project.id).music.track_key == CALM
    assert store.load(project.id).music.enabled is True


def test_re_submitting_the_choice_it_already_has_takes_no_lock_and_writes_nothing(
    app, client, store, library, locks_taken
):
    """`run_pipeline` holds this `flock` for a whole render. A no-op must not want it."""
    project = _previewed(app, store)
    client.post(f"/projects/{project.id}/music", data=_pick(track_key=CALM))
    written = _stamp(store.path_for(project.id) / "project.json")
    locks_taken.clear()

    client.post(f"/projects/{project.id}/music", data=_pick(track_key=CALM))

    assert locks_taken == [], "a no-op music write took the lock a render could hold"
    assert _stamp(store.path_for(project.id) / "project.json") == written


def test_a_real_change_does_take_the_lock(app, client, store, library, locks_taken):
    project = _previewed(app, store)
    locks_taken.clear()

    client.post(f"/projects/{project.id}/music", data=_pick(track_key=UPBEAT))

    assert locks_taken == [project.id]


def test_a_track_that_is_not_in_the_library_is_refused_and_nothing_is_written(
    app, client, store, library, locks_taken
):
    """A stale option — a renamed or deleted file — must not silently lose the bed."""
    project = _previewed(app, store)
    locks_taken.clear()

    response = client.post(
        f"/projects/{project.id}/music",
        data=_pick(track_key=f"{MUSIC_KIND}/calm/gone.wav"),
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert 'data-music-problem="unknown-track"' in response.text
    assert store.load(project.id).music.track_key == ""
    assert locks_taken == []


def test_turning_the_music_off_is_a_decision_the_project_records(
    app, client, store, library
):
    project = _previewed(app, store)
    client.post(f"/projects/{project.id}/music", data=_pick(track_key=CALM))

    client.post(f"/projects/{project.id}/music", data={**_pick(track_key=CALM), "enabled": ""})

    stored = store.load(project.id).music
    assert stored.enabled is False
    # The choice is kept, so switching it back on does not lose the track.
    assert stored.track_key == CALM


def test_a_level_equal_to_the_configured_default_is_stored_as_no_opinion(
    app, client, store, library
):
    """Empty means "no opinion" on every field of `MusicSelection`, and the slider
    has to be able to get back there — otherwise the first drag pins the project to
    a number `config.yaml` can never move again."""
    settings = app.state.settings
    project = _previewed(app, store)

    client.post(
        f"/projects/{project.id}/music",
        data=_pick(track_key=CALM, volume_db="-24", duck_db="18"),
    )
    louder = store.load(project.id).music
    client.post(
        f"/projects/{project.id}/music",
        data=_pick(
            track_key=CALM,
            volume_db=str(settings.music_volume_db),
            duck_db=str(settings.duck_amount_db),
        ),
    )

    assert (louder.volume_db, louder.duck_db) == (-24.0, 18.0)
    back = store.load(project.id).music
    assert back.volume_db is None
    assert back.duck_db is None


def test_the_sfx_toggle_is_a_project_override_that_the_default_clears(
    app, client, store, library
):
    project = _previewed(app, store)
    assert store.load(project.id).music.sfx_enabled is None

    client.post(f"/projects/{project.id}/music", data={**_pick(), "sfx": ""})
    off = store.load(project.id).music.sfx_enabled
    client.post(f"/projects/{project.id}/music", data=_pick())

    assert off is False
    assert store.load(project.id).music.sfx_enabled is None


def test_the_picker_answers_htmx_with_its_own_section_and_the_panels_out_of_band(
    app, client, store, library
):
    """A new bed makes both proxies stale, and the page has to say so at once.

    Nothing inside the project folder moves when the track changes, so an htmx swap
    of the picker alone would leave two panels still stamped `ready` under a
    freshly-named track — the page contradicting itself at the moment it was used.
    """
    project = _previewed(app, store)

    response = client.post(
        f"/projects/{project.id}/music",
        data=_pick(track_key=_other_track(app, project.id)),
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert 'id="music-picker"' in response.text
    assert 'id="preview-cuts"' in response.text
    assert "hx-swap-oob" in response.text
    assert _cuts(response.text)["wide"] == "stale"
    # A fragment, not a whole page: the swap replaces two known elements.
    assert "<html" not in response.text.lower()


def test_the_form_selection_is_decided_once_and_purely(app):
    """The parsing is a pure function, so the route has no second opinion in it."""
    settings = app.state.settings

    unchanged = music_selection(
        MusicSelection(),
        settings,
        enabled="on",
        track_key="",
        volume_db=str(settings.music_volume_db),
        duck_db=str(settings.duck_amount_db),
        sfx="on",
    )
    junk = music_selection(
        MusicSelection(),
        settings,
        enabled="on",
        track_key="",
        volume_db="not a number",
        duck_db="",
        sfx="on",
    )
    clamped = music_selection(
        MusicSelection(), settings, enabled="on", track_key="", volume_db="900",
        duck_db="-40", sfx="",
    )

    assert unchanged == MusicSelection()
    assert junk == MusicSelection()
    assert clamped.volume_db == 0.0
    assert clamped.duck_db == 0.0
    assert clamped.sfx_enabled is False


def test_changing_the_music_leaves_every_approval_alone(app, client, store, library):
    """Music is downstream of every gate: it changes the render, not what was judged.

    `clear_stale_approvals` walks the stages *upstream* of each gate, and nothing
    upstream of any gate hashes `project.music`. The preview panel going stale is
    what tells the reviewer to listen again — clearing a stamp would not.
    """
    project = _previewed(app, store)
    client.post(f"/projects/{project.id}/approve/preview", follow_redirects=False)
    stamped = store.load(project.id).approvals

    client.post(f"/projects/{project.id}/music", data=_pick(track_key=CALM))

    assert store.load(project.id).approvals == stamped


# ------------------------------------------------------- both cuts, side by side


def test_the_page_shows_both_cuts_with_their_own_states(app, client, store):
    project = _assembled(app, store)
    deps = build_deps(app.state.settings, project.id)
    for aspect in PREVIEW_ASPECTS:
        build_preview(deps.store.load(project.id), deps, aspect=aspect)

    body = client.get(f"/projects/{project.id}/preview").text

    assert _cuts(body) == {"wide": "ready", "vertical": "ready"}
    for aspect in PREVIEW_ASPECTS:
        assert f"/media/{project.id}/{preview_relpath(aspect)}" in body
        assert client.get(f"/media/{project.id}/{preview_relpath(aspect)}").status_code == 200
    # The wide cut still answers the marker every other test on this page reads.
    assert 'data-preview="ready"' in body


def test_an_empty_short_says_why_there_is_no_vertical_preview(app, client, store):
    """Task 6's `ShortNotRenderable`, surfaced on the page instead of a 500."""
    project = _assembled(app, store)
    for scene in project.scenes:
        scene.in_short = False
    store.save(project)

    response = client.get(f"/projects/{project.id}/preview")

    assert response.status_code == 200
    assert _cuts(response.text)["vertical"] == "blocked"
    assert "nothing is marked for the Short" in response.text
    assert _cuts(response.text)["wide"] in {"missing", "ready", "stale"}


def test_the_build_job_encodes_both_proxies(app, store):
    project = _assembled(app, store)

    with TestClient(app) as client:
        client.post(f"/projects/{project.id}/preview/build")
        assert app.state.jobs.wait_idle(600)
        job = app.state.jobs.state_for(project.id)
        assert job.state == "done", job.error

    root = store.path_for(project.id)
    for aspect in PREVIEW_ASPECTS:
        assert (root / preview_relpath(aspect)).is_file(), aspect


def test_the_build_job_still_finishes_the_wide_cut_when_the_short_is_empty(app, store):
    """One unrenderable aspect must not cost the reviewer the cut that *is* ready."""
    project = _assembled(app, store)
    for scene in project.scenes:
        scene.in_short = False
    store.save(project)

    with TestClient(app) as client:
        client.post(f"/projects/{project.id}/preview/build")
        assert app.state.jobs.wait_idle(600)
        job = app.state.jobs.state_for(project.id)
        assert job.state == "done", job.error

    root = store.path_for(project.id)
    assert (root / preview_relpath(Aspect.WIDE)).is_file()
    assert not (root / preview_relpath(Aspect.VERTICAL)).is_file()


# ------------------------------------------- what a new track does, and does not do


def test_changing_the_track_re_renders_and_restages_nothing_earlier(app, store, library):
    """The audio layer's whole reach into the cache is `render_hash` — proved by mtime."""
    project = _previewed(app, store)
    root = store.path_for(project.id)
    watched = {
        "video": root / video_relpath(Aspect.WIDE),
        "narration": root / narration_relpath(Aspect.WIDE),
        "captions": root / caption_relpath(Aspect.WIDE),
        "wide": root / output_relpath(Aspect.WIDE),
        "vertical": root / output_relpath(Aspect.VERTICAL),
    }

    with TestClient(app) as client:
        client.post(f"/projects/{project.id}/approve/preview")
        assert app.state.jobs.wait_idle(900)
        assert app.state.jobs.state_for(project.id).state == "done"

        before = {name: _stamp(path) for name, path in watched.items()}
        cache_path = stage_cache_for(store, project.id).path
        status_before = {
            key: value
            for key, value in json.loads(cache_path.read_text()).items()
            if key.startswith("status:")
        }

        other = _other_track(app, project.id)
        client.post(f"/projects/{project.id}/music", data=_pick(track_key=other))
        deps = build_deps(app.state.settings, project.id)
        run_pipeline(deps.store.load(project.id), deps)

    after = {name: _stamp(path) for name, path in watched.items()}
    assert after["wide"] != before["wide"], "a new bed must re-encode the deliverable"
    assert after["vertical"] != before["vertical"]
    for name in ("video", "narration", "captions"):
        assert after[name] == before[name], f"{name} was rebuilt for a music change"

    reloaded = deps.store.load(project.id)
    fresh_cache = stage_cache_for(store, project.id)
    status_after = {
        key: value
        for key, value in json.loads(cache_path.read_text()).items()
        if key.startswith("status:")
    }
    assert status_after == status_before, "a music change moved an upstream fingerprint"
    for stage in ("script", "voice", "align", "visuals", "captions", "assemble"):
        assert stage_is_current(reloaded, fresh_cache, stage), stage
