"""Gate 2: the storyboard page, the candidate swap, motion, re-voice and approve.

**The test that earns this file's keep is `test_swapping_one_visual_re_encodes_only_that_scene`.**
It is the M2 definition of done — *editing one scene at storyboard re-generates
only that scene* — and it is proved the way M1 proved its own: every
`run_ffmpeg` at both call sites is counted across a real re-run, and the mtimes
of the segments that must not move are witnessed either side of it. A swap has to
invalidate exactly one segment, the join, the narration bed and the final render;
anything more and the "<10 s re-run" promise is gone, anything less and the
finished video still shows the shot the editor rejected.

Two things make that test say something rather than merely pass:

* **The candidates carry different bytes.** `MockStock.download` normally copies
  one checked-in fixture for every hit, so swapping candidates would leave the
  segment's inputs byte-identical and `assemble` would be *right* to skip the
  re-encode — the test would pass without exercising anything. The stub here
  writes a genuinely different clip per `source_id`, which is what real stock
  candidates are, and lets real FFmpeg encode them.
* **The narrations are shortened first.** The mock TTS speaks 0.4 s per word and
  the mock LLM writes long sentences; trimming them keeps the two real renders in
  this file at a few seconds each without weakening anything being asserted.

The other properties pinned here:

* **A not-yet-downloaded candidate is fetched on demand.** M1's visuals stage
  downloads only the chosen hit, so `candidates[1:]` have `local_path == ""`.
  That is the *common* case for this page, so it has its own test, and the
  downloaded file has to be servable through `/media/...`.
* **A choice that changes nothing writes nothing** — bytes, mtime and inode of
  `project.json`, the same three witnesses gate 1 uses, because `save()` is an
  atomic `os.replace`.
* **A swap clears the preview approval and leaves the storyboard one alone.**
  M1's `clear_stale_approvals` decides that; this file asserts the page obeys it.
* **The handlers never run a stage** (design decision 2): re-voice enqueues a
  `revoice` job and approving enqueues a run, and with no lifespan running there
  is no worker to hide an inline `run_pipeline` behind.

Everything is on the mock provider chain: no network, no `ml` extra.
"""

import hashlib
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from videomaker import runner as runner_module
from videomaker.config import Settings
from videomaker.models import Aspect, AssetRef, Motion
from videomaker.pipeline import assemble as assemble_module
from videomaker.pipeline import render as render_module
from videomaker.pipeline.assemble import ASSEMBLE_ASPECTS, segment_relpath
from videomaker.pipeline.render import output_relpath
from videomaker.project import PROJECT_FILE, ProjectStore
from videomaker.providers import mock as mock_module
from videomaker.providers.assets import project_relative
from videomaker.runner import build_deps, run_pipeline
from videomaker.web.app import create_app

#: Every absolute URL the page emits, and the subset of them that would be a
#: no-CDN violation. Candidate thumbnails are remote by necessity (M3 Task 2), so
#: the guard here is "no external *code or styles*", checked against the values.
_ABSOLUTE_URL_VALUE = re.compile(r'https?://[^"\s<>]+', re.IGNORECASE)
_EXTERNAL_CODE = re.compile(r"<(?:script|link)\b[^>]*https?://", re.IGNORECASE)

#: One card per scene, and one tile per candidate inside it. The two data
#: attributes of a tile are kept adjacent in the template so this can read them.
_CARD = re.compile(r'data-scene="(s\d+)"')
_CANDIDATE = re.compile(r'data-candidate="(\d+)" data-chosen="(true|false)"')
_MOTION_SELECT = re.compile(r'<select[^>]*\bname="motion"')

#: Same `data-gate`/`data-approved` contract the dashboard and gate 1 use.
_GATE = re.compile(r'data-gate="([a-z]+)" data-approved="(true|false)"')

#: What htmx puts on every request it makes.
_HX = {"HX-Request": "true"}

#: Short enough that the two real renders in this file stay a few seconds.
SHORT_NARRATION = "Scene {number} says a few short words."

SWAPPED_SCENE = "s02"
SWAPPED_INDEX = 1


# ------------------------------------------------------- distinguishable candidates


def _variant_clip(path: Path, source_id: str) -> None:
    """A real 8 s clip whose colour — and therefore whose bytes — is unique to `source_id`."""
    colour = "0x" + hashlib.sha256(source_id.encode()).hexdigest()[:6]
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error",
            "-f", "lavfi",
            "-i", f"color=c={colour}:s=640x360:r=30:d={mock_module.CLIP_DURATION_S}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _variant_download(_self, result, out_path, *, max_height: int = 1080) -> AssetRef:
    """`MockStock.download` that gives every hit its own bytes, not one shared fixture."""
    path = Path(out_path)
    _variant_clip(path, result.source_id)
    return AssetRef(
        provider=mock_module.PROVIDER_NAME,
        source_id=result.source_id,
        source_url=result.source_url,
        local_path=project_relative(path),
        width=640,
        height=360,
        duration_s=mock_module.CLIP_DURATION_S,
        attribution=result.attribution,
        license=result.license,
    )


# ------------------------------------------------------------------- fixtures


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    # Keep the response cache and the quota ledger inside the test's own tmp dir:
    # the choose handler builds real `StageDeps` to reach the stock provider.
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
def downloads(monkeypatch) -> list[str]:
    """Every `MockStock.download`, by source id — the choose path's fetch is one of these."""
    calls: list[str] = []
    original = mock_module.MockStock.download

    def counting(self, result, out_path, **kwargs):
        calls.append(result.source_id)
        return original(self, result, out_path, **kwargs)

    monkeypatch.setattr(mock_module.MockStock, "download", counting)
    return calls


@pytest.fixture
def locks_taken(monkeypatch) -> list[str]:
    """Every `ProjectStore.lock` the code under test opens.

    An action with nothing to do must not appear here at all: `run_pipeline`
    holds this same `flock` for a whole render, so a handler that locked
    unconditionally would park a request thread for minutes at a time.
    """
    taken: list[str] = []
    original = ProjectStore.lock

    def spying_lock(self: ProjectStore, project_id: str):
        taken.append(project_id)
        return original(self, project_id)

    monkeypatch.setattr(ProjectStore, "lock", spying_lock)
    return taken


def _new(store: ProjectStore, topic: str = "how ssds work"):
    return store.create(topic, "tech_explainer", target_minutes=0.5)


def _storyboarded(app, store: ProjectStore, *, until: str = "visuals"):
    """A project run as far as `until`, so there are real candidates to review."""
    project = _new(store)
    run_pipeline(project, build_deps(app.state.settings, project.id), until=until, yes=True)
    return store.load(project.id)


def _card_html(body: str, scene_id: str) -> str:
    """The markup of one scene's card.

    Sliced at the *next* card's marker rather than at a closing tag: a card
    contains a nested list of candidate tiles, so the first `</li>` after the
    marker is somewhere in the middle of it.
    """
    chunk = body.split(f'data-scene="{scene_id}"', 1)[1]
    return chunk.split('data-scene="', 1)[0]


def _on_disk(store: ProjectStore, project_id: str) -> tuple[bytes, int, int]:
    """`project.json`'s bytes, mtime **and inode**: `save()` is an atomic `os.replace`."""
    stat_path = store.path_for(project_id) / PROJECT_FILE
    stat = stat_path.stat()
    return (stat_path.read_bytes(), stat.st_mtime_ns, stat.st_ino)


# --------------------------------------------------------------- GET the page


def test_the_storyboard_page_of_an_unknown_project_is_a_404(client):
    assert client.get("/projects/no-such-project/storyboard").status_code == 404


def test_the_page_has_one_card_per_scene(app, client, store):
    project = _storyboarded(app, store)

    body = client.get(f"/projects/{project.id}/storyboard").text

    assert len(project.scenes) >= 2
    assert _CARD.findall(body) == [scene.id for scene in project.scenes]


def test_each_card_shows_the_chosen_visual_through_the_media_route(app, client, store):
    project = _storyboarded(app, store)

    body = client.get(f"/projects/{project.id}/storyboard").text

    for scene in project.scenes:
        chosen = scene.visual.chosen
        assert chosen is not None
        assert f"/media/{project.id}/{chosen.local_path}" in body


def test_the_chosen_visual_is_actually_servable(app, client, store):
    project = _storyboarded(app, store)
    chosen = project.scenes[0].visual.chosen

    response = client.get(f"/media/{project.id}/{chosen.local_path}")

    assert response.status_code == 200
    assert response.content


def test_every_candidate_is_offered_and_exactly_one_is_marked_chosen(app, client, store):
    project = _storyboarded(app, store)
    scene = project.scenes[0]

    body = client.get(f"/projects/{project.id}/storyboard").text
    tiles = _CANDIDATE.findall(_card_html(body, scene.id))

    assert len(scene.visual.candidates) >= 2
    assert [index for index, _ in tiles] == [
        str(index) for index in range(len(scene.visual.candidates))
    ]
    assert [chosen for _, chosen in tiles].count("true") == 1


def test_each_card_offers_motion_and_re_voice(app, client, store):
    project = _storyboarded(app, store)

    body = client.get(f"/projects/{project.id}/storyboard").text

    assert len(_MOTION_SELECT.findall(body)) == len(project.scenes)
    for scene in project.scenes:
        assert f'action="/projects/{project.id}/scenes/{scene.id}/revoice"' in body
        assert f'action="/projects/{project.id}/scenes/{scene.id}/choose"' in body


def test_the_page_offers_the_approve_action(app, client, store):
    project = _storyboarded(app, store)

    body = client.get(f"/projects/{project.id}/storyboard").text

    assert f'action="/projects/{project.id}/approve/storyboard"' in body
    assert dict(_GATE.findall(body))["storyboard"] == "false"


def test_a_project_with_no_visuals_yet_still_renders(client, store):
    project = _new(store)

    response = client.get(f"/projects/{project.id}/storyboard")

    assert response.status_code == 200
    assert _CARD.findall(response.text) == []
    assert "No storyboard yet" in response.text


def test_an_offered_candidate_shows_the_providers_thumbnail(app, client, store):
    """The grey `clip · 1920×1080 · 25s` placeholder is the fallback, not the norm.

    Only the chosen hit is downloaded, so every other tile has no local file. It
    still has the provider's thumbnail, and that is what the tile must draw.
    """
    project = _storyboarded(app, store)
    scene = store.load(project.id).scene_by_id(SWAPPED_SCENE)
    offers = [ref for ref in scene.visual.candidates if not ref.local_path]
    assert offers, "the fixture must actually offer un-downloaded alternatives"

    card = _card_html(client.get(f"/projects/{project.id}/storyboard").text, SWAPPED_SCENE)

    for ref in offers:
        assert f'src="{ref.preview_url}"' in card
    # Nothing is left showing the placeholder: every tile now has a picture.
    assert "candidate-blank" not in card


def test_a_candidate_with_no_thumbnail_still_falls_back_to_the_placeholder(app, client, store):
    """The pre-`preview_url` projects on disk have neither a file nor a thumbnail."""
    project = _storyboarded(app, store)
    saved = store.load(project.id)
    scene = saved.scene_by_id(SWAPPED_SCENE)
    scene.visual.candidates = [
        ref.model_copy(update={"preview_url": "", "local_path": ""})
        for ref in scene.visual.candidates
    ]
    store.save(saved)

    card = _card_html(client.get(f"/projects/{project.id}/storyboard").text, SWAPPED_SCENE)

    assert "candidate-blank" in card
    assert "<img" not in card


def test_the_page_loads_no_external_code_or_styles(app, client, store):
    """The no-CDN guard, narrowed by M3 Task 2.

    Provider thumbnails are now drawn from their remote URLs — there is no local
    copy of an un-downloaded alternative to serve instead — so the guard names
    what may be external (a candidate's `preview_url`, nothing else) rather than
    banning every absolute URL.
    """
    project = _storyboarded(app, store)

    response = client.get(f"/projects/{project.id}/storyboard")

    assert response.status_code == 200
    body = response.text
    assert not _EXTERNAL_CODE.search(body), "no script or stylesheet may come from off-box"
    previews = {
        ref.preview_url
        for scene in store.load(project.id).scenes
        for ref in scene.visual.candidates
        if ref.preview_url
    }
    assert previews, "the fixture must actually carry remote thumbnails"
    assert set(_ABSOLUTE_URL_VALUE.findall(body)) <= previews


# ------------------------------------------------------- POST a chosen candidate


def test_choosing_a_candidate_downloads_it_first(app, client, store, downloads):
    """M1 downloads only the chosen hit, so this is the *common* path, not an edge case."""
    project = _storyboarded(app, store)
    scene = project.scene_by_id(SWAPPED_SCENE)
    wanted = scene.visual.candidates[SWAPPED_INDEX]
    assert wanted.local_path == ""
    downloads.clear()

    response = client.post(
        f"/projects/{project.id}/scenes/{SWAPPED_SCENE}/choose",
        data={"candidate_index": SWAPPED_INDEX},
        headers=_HX,
    )

    assert response.status_code == 200
    assert downloads == [wanted.source_id]
    saved = store.load(project.id).scene_by_id(SWAPPED_SCENE)
    assert saved.visual.chosen.source_id == wanted.source_id
    assert saved.visual.candidates[SWAPPED_INDEX].local_path
    assert (store.path_for(project.id) / saved.visual.chosen.local_path).is_file()


def test_the_downloaded_candidate_is_servable_and_shown_on_the_page(app, client, store):
    project = _storyboarded(app, store)

    client.post(
        f"/projects/{project.id}/scenes/{SWAPPED_SCENE}/choose",
        data={"candidate_index": SWAPPED_INDEX},
        headers=_HX,
    )

    chosen = store.load(project.id).scene_by_id(SWAPPED_SCENE).visual.chosen
    assert client.get(f"/media/{project.id}/{chosen.local_path}").status_code == 200
    body = client.get(f"/projects/{project.id}/storyboard").text
    assert f"/media/{project.id}/{chosen.local_path}" in body


def test_choosing_leaves_the_previous_candidates_file_in_place(app, client, store):
    """The old shot stays downloaded, so swapping back costs nothing."""
    project = _storyboarded(app, store)
    original = project.scene_by_id(SWAPPED_SCENE).visual.chosen.local_path

    client.post(
        f"/projects/{project.id}/scenes/{SWAPPED_SCENE}/choose",
        data={"candidate_index": SWAPPED_INDEX},
        headers=_HX,
    )

    assert (store.path_for(project.id) / original).is_file()
    saved = store.load(project.id).scene_by_id(SWAPPED_SCENE)
    assert saved.visual.chosen.local_path != original


def test_choosing_returns_the_scene_card_partial_for_htmx(app, client, store):
    project = _storyboarded(app, store)

    response = client.post(
        f"/projects/{project.id}/scenes/{SWAPPED_SCENE}/choose",
        data={"candidate_index": SWAPPED_INDEX},
        headers=_HX,
    )

    assert response.status_code == 200
    assert "<!doctype" not in response.text.lower()
    assert f'data-scene="{SWAPPED_SCENE}"' in response.text
    # The gate card rides along out of band, so news of a cleared approval arrives
    # without a reload.
    assert 'hx-swap-oob="true"' in response.text


def test_a_plain_form_post_redirects_back_to_the_page(app, client, store):
    project = _storyboarded(app, store)

    response = client.post(
        f"/projects/{project.id}/scenes/{SWAPPED_SCENE}/choose",
        data={"candidate_index": SWAPPED_INDEX},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == (
        f"/projects/{project.id}/storyboard#scene-{SWAPPED_SCENE}"
    )


def test_choosing_the_candidate_already_chosen_writes_nothing(
    app, client, store, downloads, locks_taken
):
    project = _storyboarded(app, store)
    before = _on_disk(store, project.id)
    downloads.clear()
    locks_taken.clear()

    response = client.post(
        f"/projects/{project.id}/scenes/{SWAPPED_SCENE}/choose",
        data={"candidate_index": 0},
        headers=_HX,
    )

    assert response.status_code == 200
    assert _on_disk(store, project.id) == before
    assert downloads == []
    # Not even reached for: the compare happens before the lock, on purpose.
    assert locks_taken == []


def test_an_out_of_range_candidate_is_rejected(app, client, store):
    project = _storyboarded(app, store)
    count = len(project.scene_by_id(SWAPPED_SCENE).visual.candidates)

    response = client.post(
        f"/projects/{project.id}/scenes/{SWAPPED_SCENE}/choose",
        data={"candidate_index": count},
        headers=_HX,
    )

    assert response.status_code == 422


def test_choosing_for_an_unknown_scene_is_a_404(app, client, store):
    project = _storyboarded(app, store)

    response = client.post(
        f"/projects/{project.id}/scenes/s99/choose", data={"candidate_index": 0}, headers=_HX
    )

    assert response.status_code == 404


# ------------------------------------------------------------------ POST motion


def test_setting_the_motion_persists_it(app, client, store):
    project = _storyboarded(app, store)
    assert project.scene_by_id("s01").visual.motion is not Motion.ZOOM

    response = client.post(
        f"/projects/{project.id}/scenes/s01/motion", data={"motion": "zoom"}, headers=_HX
    )

    assert response.status_code == 200
    saved = store.load(project.id)
    assert saved.scene_by_id("s01").visual.motion is Motion.ZOOM
    # The neighbour keeps whatever it had.
    assert saved.scene_by_id("s02").visual.motion is project.scene_by_id("s02").visual.motion


def test_setting_the_motion_it_already_has_writes_nothing(app, client, store, locks_taken):
    project = _storyboarded(app, store)
    current = project.scene_by_id("s01").visual.motion
    before = _on_disk(store, project.id)
    locks_taken.clear()

    response = client.post(
        f"/projects/{project.id}/scenes/s01/motion", data={"motion": current.value}, headers=_HX
    )

    assert response.status_code == 200
    assert _on_disk(store, project.id) == before
    assert locks_taken == []


def test_an_unknown_motion_is_rejected(app, client, store):
    project = _storyboarded(app, store)

    response = client.post(
        f"/projects/{project.id}/scenes/s01/motion", data={"motion": "somersault"}, headers=_HX
    )

    assert response.status_code == 422
    assert store.load(project.id).scene_by_id("s01").visual.motion is (
        project.scene_by_id("s01").visual.motion
    )


# ----------------------------------------------------------------- POST revoice


def test_re_voicing_enqueues_a_job_and_does_not_run_it(app, client, store):
    project = _storyboarded(app, store)
    wav = store.path_for(project.id) / project.scene_by_id("s01").audio_path
    before = wav.stat().st_mtime_ns

    response = client.post(
        f"/projects/{project.id}/scenes/s01/revoice", follow_redirects=False
    )

    assert response.status_code == 303
    job = app.state.jobs.state_for(project.id)
    assert job is not None
    assert (job.kind, job.state) == ("revoice", "queued")
    # No worker is running, so a stage run inside the handler is the only way this
    # file could have moved.
    assert wav.stat().st_mtime_ns == before


def test_the_revoice_job_re_speaks_only_that_scene(app, store, monkeypatch):
    """The job body itself: run it on this thread and watch which wavs move."""
    from videomaker.web.routes import storyboard as storyboard_routes
    from videomaker.web.worker import JobProgress, JobQueue

    project = _storyboarded(app, store)
    root = store.path_for(project.id)
    target = root / project.scene_by_id("s02").audio_path
    untouched = root / project.scene_by_id("s01").audio_path
    before_target = target.stat().st_mtime_ns
    before_untouched = untouched.stat().st_mtime_ns
    time.sleep(0.01)

    job = storyboard_routes.revoice_job(app.state.settings, project.id, "s02")
    job(JobProgress(JobQueue(), project.id))

    assert target.stat().st_mtime_ns != before_target
    assert untouched.stat().st_mtime_ns == before_untouched


def test_re_voicing_an_unknown_scene_is_a_404(app, client, store):
    project = _storyboarded(app, store)

    assert client.post(f"/projects/{project.id}/scenes/s99/revoice").status_code == 404


# ----------------------------------------------------------------- POST approve


def test_approving_stamps_the_gate_and_enqueues_a_run(app, client, store):
    project = _storyboarded(app, store)
    assert project.approvals.storyboard is None

    response = client.post(
        f"/projects/{project.id}/approve/storyboard", follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/projects/{project.id}"
    assert store.load(project.id).approvals.storyboard is not None
    job = app.state.jobs.state_for(project.id)
    assert job is not None
    assert (job.kind, job.state) == ("run", "queued")


def test_approving_twice_keeps_the_first_stamp(app, client, store):
    project = _storyboarded(app, store)

    client.post(f"/projects/{project.id}/approve/storyboard")
    first = store.load(project.id).approvals.storyboard
    client.post(f"/projects/{project.id}/approve/storyboard")

    assert first is not None
    assert store.load(project.id).approvals.storyboard == first


def test_approving_does_not_run_the_pipeline_in_the_handler(app, client, store):
    """No lifespan means no worker, so anything built here was built inline."""
    project = _storyboarded(app, store)
    root = store.path_for(project.id)

    client.post(f"/projects/{project.id}/approve/storyboard")

    # `store.create` lays out the folders, so emptiness is the witness, not absence.
    assert list((root / "build").iterdir()) == []
    assert not (root / output_relpath(Aspect.WIDE)).exists()


def test_approving_an_unknown_project_is_a_404(client):
    assert client.post("/projects/nope/approve/storyboard").status_code == 404


# ------------------------------------------------------- the M2 definition of done


@dataclass(frozen=True)
class Phase:
    """What one `run_pipeline` did, frozen the moment it finished."""

    ffmpeg_calls: list[list[str]] = field(default_factory=list)
    #: scene id -> mtime of `build/<scene>_wide.mp4`, in nanoseconds.
    segments: dict[str, int] = field(default_factory=dict)
    final_mtime: int = 0
    scene_ids: list[str] = field(default_factory=list)

    @property
    def segment_encodes(self) -> list[str]:
        """The per-scene encodes only — not the join, the narration bed or the render."""
        wanted = {segment_relpath(scene_id, Aspect.WIDE) for scene_id in self.scene_ids}
        return [args[-1] for args in self.ffmpeg_calls if args[-1] in wanted]


@dataclass(frozen=True)
class Swap:
    """The whole journey: render, swap one scene's visual, render again."""

    project_id: str
    store: ProjectStore
    first: Phase
    second: Phase
    downloads: list[str]
    approvals_before: tuple[bool, bool, bool]
    approvals_after: tuple[bool, bool, bool]
    chosen_before: str
    chosen_after: str


def _mtime_ns(path: Path) -> int:
    return path.stat().st_mtime_ns if path.is_file() else 0


def _observe(store: ProjectStore, project_id: str, calls: list[list[str]]) -> Phase:
    project = store.load(project_id)
    root = store.path_for(project_id)
    return Phase(
        ffmpeg_calls=[list(args) for args in calls],
        segments={
            scene.id: _mtime_ns(root / segment_relpath(scene.id, Aspect.WIDE))
            for scene in project.scenes
        },
        final_mtime=_mtime_ns(root / output_relpath(Aspect.WIDE)),
        scene_ids=[scene.id for scene in project.scenes],
    )


def _approval_marks(store: ProjectStore, project_id: str) -> tuple[bool, bool, bool]:
    approvals = store.load(project_id).approvals
    return (
        approvals.script is not None,
        approvals.storyboard is not None,
        approvals.preview is not None,
    )


@pytest.fixture(scope="module")
def swap(tmp_path_factory) -> Swap:
    """Render a project for real, swap scene two's visual through the web UI, render again."""
    tmp_path = tmp_path_factory.mktemp("storyboard_dod")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(runner_module, "USER_CACHE_DIR", tmp_path / "cache")
        # Distinguishable candidates: one shared fixture would make the swap a
        # byte-for-byte no-op and this whole test vacuous.
        patch.setattr(mock_module.MockStock, "download", _variant_download)

        downloads: list[str] = []
        original_download = mock_module.MockStock.download

        def counting_download(self, result, out_path, **kwargs):
            downloads.append(result.source_id)
            return original_download(self, result, out_path, **kwargs)

        patch.setattr(mock_module.MockStock, "download", counting_download)

        calls: list[list[str]] = []
        original_ffmpeg = assemble_module.run_ffmpeg

        def counting_ffmpeg(args, **kwargs):
            calls.append(list(args))
            return original_ffmpeg(args, **kwargs)

        for module in (assemble_module, render_module):
            patch.setattr(module, "run_ffmpeg", counting_ffmpeg)

        settings = Settings(workspace_dir=tmp_path / "workspace")
        app = create_app(settings, providers="mock")
        settings = app.state.settings
        store: ProjectStore = app.state.store

        project = store.create("how ssds work", "tech_explainer", target_minutes=0.5)
        run_pipeline(project, build_deps(settings, project.id), until="script", yes=True)

        # Short narrations keep the two real renders below to a few seconds each.
        project = store.load(project.id)
        for number, scene in enumerate(project.scenes, start=1):
            scene.narration = SHORT_NARRATION.format(number=number)
        store.save(project)

        calls.clear()
        run_pipeline(store.load(project.id), build_deps(settings, project.id), yes=True)
        first = _observe(store, project.id, calls)
        approvals_before = _approval_marks(store, project.id)
        chosen_before = store.load(project.id).scene_by_id(SWAPPED_SCENE).visual.chosen.local_path

        # mtime witnesses need a clock tick they can actually resolve.
        time.sleep(0.01)

        downloads.clear()
        with TestClient(app) as client:
            response = client.post(
                f"/projects/{project.id}/scenes/{SWAPPED_SCENE}/choose",
                data={"candidate_index": SWAPPED_INDEX},
                headers=_HX,
            )
            assert response.status_code == 200, response.text
            client.app.state.jobs.wait_idle(30)

        approvals_after = _approval_marks(store, project.id)
        chosen_after = store.load(project.id).scene_by_id(SWAPPED_SCENE).visual.chosen.local_path

        calls.clear()
        swapped = list(downloads)
        downloads.clear()
        run_pipeline(store.load(project.id), build_deps(settings, project.id), yes=True)
        second = _observe(store, project.id, calls)

        return Swap(
            project_id=project.id,
            store=store,
            first=first,
            second=second,
            downloads=swapped + downloads,
            approvals_before=approvals_before,
            approvals_after=approvals_after,
            chosen_before=chosen_before,
            chosen_after=chosen_after,
        )


def test_the_first_run_really_rendered_everything(swap):
    """The premise of every assertion below: there was a finished video to disturb."""
    assert swap.first.final_mtime > 0
    assert len(swap.first.scene_ids) >= 3
    assert sorted(swap.first.segment_encodes) == sorted(
        segment_relpath(scene_id, Aspect.WIDE) for scene_id in swap.first.scene_ids
    )
    assert all(mtime > 0 for mtime in swap.first.segments.values())


def test_swapping_one_visual_re_encodes_only_that_scene(swap):
    """**The M2 definition of done.**

    One candidate swapped at gate 2 must re-encode that scene's segment, re-join,
    rebuild the narration bed and re-render the final file — and touch nothing
    else. The encodes are counted at `run_ffmpeg` itself and the segments that
    must not move are witnessed by mtime, so a handler that quietly invalidated
    the whole assemble stage could not pass by producing a correct video slowly.
    """
    assert swap.chosen_after != swap.chosen_before

    # Exactly one segment re-encode, and it is the swapped scene's.
    assert swap.second.segment_encodes == [segment_relpath(SWAPPED_SCENE, Aspect.WIDE)]

    # Every other segment is the *same file*, untouched on disk.
    for scene_id, mtime in swap.first.segments.items():
        if scene_id == SWAPPED_SCENE:
            assert swap.second.segments[scene_id] != mtime
        else:
            assert swap.second.segments[scene_id] == mtime

    # The re-run is the segment, the join, the narration bed and the render — once per
    # aspect, because the Short is re-cut from the very same swapped shot. Nothing more.
    assert len(swap.second.ffmpeg_calls) == 4 * len(ASSEMBLE_ASPECTS)
    assert swap.second.final_mtime != swap.first.final_mtime

    # And the new shot was fetched exactly once, by the web handler, not by a stage.
    assert len(swap.downloads) == 1


def test_swapping_clears_the_preview_approval_and_keeps_the_storyboard_one(swap):
    """`clear_stale_approvals` decides this; the page must not second-guess it."""
    assert swap.approvals_before == (True, True, True)
    script, storyboard, preview = swap.approvals_after
    assert (script, storyboard) == (True, True)
    assert preview is False
