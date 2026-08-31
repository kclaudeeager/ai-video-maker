"""The M2 definition of done, end to end, **through the browser**.

`tests/integration/test_golden_path.py` proves M1's promise through the Typer app;
this is its sibling for M2, and it never touches the CLI. A project is created by
posting the form on `/`, its script is edited scene by scene by posting the real
`<form>`s on `/projects/{id}/script`, all three gates are cleared by posting their
approve buttons, and the finished `output/final_wide.mp4` is probed with ffprobe.
Everything in between is HTTP: `TestClient` inside its context manager, so the
lifespan runs and the single background worker really drains the queue.

**Why the counters, and not the clock.** The sentence this file exists to defend is
*editing one scene at the storyboard gate re-generates only that scene*. It is
counted at the source, exactly as M1 counts its own: every mock provider method is
wrapped, every `run_ffmpeg` call site is wrapped (there are three in M2 — assemble,
render and the 480p preview), and the per-scene segments' mtimes are witnessed
either side of the swap. A handler that quietly invalidated the whole assemble
stage would still produce a correct video, just slowly, and only the counts can
tell the difference.

**Two things stop the DoD assertion being vacuous.**

* **The candidates carry different bytes.** `MockStock.download` normally copies
  one checked-in fixture for every hit, so a swap would leave the segment's inputs
  byte-identical, `assemble`'s content hash would be *right* to skip the re-encode,
  and the assertion would pass while proving nothing. `_variant_download` below
  gives every `source_id` its own clip, which is what real stock candidates are.
  `tests/unit/test_web_storyboard.py` makes the same move for the same reason; the
  helper is duplicated rather than imported because `tests/` is not a package.
* **There is a finished video to disturb before the swap happens.** See the next
  paragraph — this is the one place the journey below departs from the plan's
  step order, and it is what makes step 5 measurable at all.

**Why the swap comes after gate 3 and not before it.** `GATE_BEFORE` is
`{"voice": "script", "captions": "storyboard", "render": "preview"}`, so a run held
at the storyboard gate has not reached `assemble`: there are no segments on disk
yet. Swapping a candidate *there* and then approving gate 2 would encode all four
segments from nothing, and "exactly one segment re-encode" would be a sentence
about a project that had never been assembled. So the journey clears all three
gates first — which is the M2 DoD in its own right, *a full project via the
browser* — and only then goes back to `/storyboard`, swaps scene two's shot and
re-advances. That is also what a reviewer actually does: you watch the thing, you
dislike one shot, you change it, and you expect the other three scenes not to be
re-encoded.

**Narrations are shortened first**, through the gate-1 form, for the same reason
M1's golden path shortens them: the mock TTS speaks 0.4 s per word and the mock LLM
writes long sentences, so trimming keeps the real encodes here to a handful of
seconds without weakening anything asserted below.

The gate-1 edits are posted **without** htmx headers and the gate-2 swap **with**
them, so both halves of the M2 no-JavaScript constraint are exercised by the same
journey: a plain form post redirects (303), an htmx post answers with the partial
(200).
"""

import hashlib
import re
import subprocess
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from videomaker import preview as preview_module
from videomaker import runner as runner_module
from videomaker.config import Settings
from videomaker.media.ffmpeg import probe_duration, probe_json
from videomaker.models import Aspect, AssetRef
from videomaker.pipeline import assemble as assemble_module
from videomaker.pipeline import render as render_module
from videomaker.pipeline.assemble import ASSEMBLE_ASPECTS, WIDE_SPEC, segment_relpath
from videomaker.pipeline.base import SCENE_GAP_S
from videomaker.pipeline.render import RENDER_ASPECTS, output_relpath
from videomaker.preview import preview_relpath
from videomaker.project import ProjectStore
from videomaker.providers import mock as mock_module
from videomaker.providers.assets import project_relative
from videomaker.web.app import create_app

TOPIC = "how ssds work"
#: `ProjectStore.create` slugs the topic; the browser only ever learns this id from
#: the redirect it is given, and the fixture asserts the two agree.
PROJECT_ID = "how-ssds-work"
TEMPLATE = "tech_explainer"
#: The shortest video the template allows (it clamps to four scenes either way).
MINUTES = "0.5"
VOICE = "af_heart"

#: Short on purpose: every encode below is real, so each spoken word is wall time.
SHORT_NARRATION = "Scene {number} says a few short words."
#: One scene also gets its visual query rewritten, so the gate-1 form's other field
#: is exercised by the journey rather than only by the unit tests.
EDITED_QUERY = "close up nand flash package"

SWAPPED_SCENE = "s02"
SWAPPED_INDEX = 1

#: How long a browser would be willing to sit on the polling page. Generous: this
#: is a timeout, not a performance assertion — the counts are the evidence.
JOB_TIMEOUT_S = 180.0

DURATION_TOLERANCE_S = 0.5

#: What htmx puts on every request it makes.
_HX = {"HX-Request": "true"}

#: The attribute pairs the templates promise the tests (see `_status.html`).
_SCENE = re.compile(r'data-scene="(s\d+)"')
_GATE = re.compile(r'data-gate="([a-z]+)" data-approved="(true|false)"')
_JOB_STATE = re.compile(r'data-job-state="(\w+)"')
_PREVIEW_STATE = re.compile(r'data-preview="(\w+)"')
_RENDER_STATE = re.compile(r'data-render="(\w+)"')

#: Every provider method `--providers mock` can reach, labelled by the kind of quota
#: a real provider would have spent making the same call. Same list as M1's golden
#: path, for the same reason: "nothing was regenerated" has to be counted somewhere.
PROVIDER_METHODS: tuple[tuple[type, str, str], ...] = (
    (mock_module.MockLLM, "generate", "llm"),
    (mock_module.MockTTS, "synthesize", "tts"),
    (mock_module.MockSTT, "transcribe_words", "stt"),
    (mock_module.MockStock, "search", "stock.search"),
    (mock_module.MockStock, "download", "stock.download"),
    (mock_module.MockImage, "generate_image", "image"),
)


# ------------------------------------------------- distinguishable candidates


def _variant_clip(path: Path, source_id: str) -> None:
    """A real clip whose colour — and so whose bytes — is unique to `source_id`."""
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


# ------------------------------------------------------------------- observation


@dataclass(frozen=True)
class Phase:
    """Everything one step of the journey did, frozen the moment it finished."""

    #: The argv of every `run_ffmpeg`, at all three call sites; last item is the output.
    ffmpeg_calls: list[list[str]] = field(default_factory=list)
    provider_calls: Counter = field(default_factory=Counter)
    #: scene id -> mtime of `build/<scene>_wide.mp4`, in nanoseconds (0 when absent).
    segments: dict[str, int] = field(default_factory=dict)
    final_mtime: int = 0
    preview_mtime: int = 0
    scene_ids: list[str] = field(default_factory=list)
    #: ffprobe's view of `output/final_wide.mp4`, empty until there is one.
    streams: dict[str, dict] = field(default_factory=dict)
    duration_s: float = 0.0
    expected_duration_s: float = 0.0

    @property
    def segment_encodes(self) -> list[str]:
        """The per-scene encodes only — not the join, the bed, the preview or the render."""
        wanted = {segment_relpath(scene_id, Aspect.WIDE) for scene_id in self.scene_ids}
        return [args[-1] for args in self.ffmpeg_calls if args[-1] in wanted]


def _mtime_ns(path: Path) -> int:
    return path.stat().st_mtime_ns if path.is_file() else 0


def _count_provider_calls(patch: pytest.MonkeyPatch) -> Counter:
    """Tally every mock provider call, so "nothing was regenerated" is a number."""
    counts: Counter = Counter()
    for cls, method, label in PROVIDER_METHODS:
        original = getattr(cls, method)

        def counting(*args, _original=original, _label=label, **kwargs):
            counts[_label] += 1
            return _original(*args, **kwargs)

        patch.setattr(cls, method, counting)
    return counts


def _count_ffmpeg_calls(patch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record every real FFmpeg invocation, at all three call sites, and still run it.

    `render` is one of them and `routes/render.encode_progress` swaps a wrapper of
    its own into that module while the final encode runs — it reads whatever is
    bound at the time, so it wraps this counter rather than replacing it, and the
    render still shows up here.
    """
    calls: list[list[str]] = []
    original = assemble_module.run_ffmpeg

    def counting(args, **kwargs):
        calls.append(list(args))
        return original(args, **kwargs)

    for module in (assemble_module, render_module, preview_module):
        patch.setattr(module, "run_ffmpeg", counting)
    return calls


def _observe(
    store: ProjectStore,
    project_id: str,
    calls: list[list[str]],
    counts: Counter,
) -> Phase:
    """Freeze what the journey has done since the counters were last cleared."""
    project = store.load(project_id)
    root = store.path_for(project_id)
    final = root / output_relpath(Aspect.WIDE)
    streams: dict[str, dict] = {}
    duration_s = 0.0
    if final.is_file():
        streams = {stream["codec_type"]: stream for stream in probe_json(final)["streams"]}
        duration_s = probe_duration(final)
    return Phase(
        ffmpeg_calls=[list(args) for args in calls],
        provider_calls=counts.copy(),
        segments={
            scene.id: _mtime_ns(root / segment_relpath(scene.id, Aspect.WIDE))
            for scene in project.scenes
        },
        final_mtime=_mtime_ns(final),
        preview_mtime=_mtime_ns(root / preview_relpath(Aspect.WIDE)),
        scene_ids=[scene.id for scene in project.scenes],
        streams=streams,
        duration_s=duration_s,
        expected_duration_s=sum(
            (scene.duration_s or 0.0) + SCENE_GAP_S for scene in project.scenes
        ),
    )


# ------------------------------------------------------------------ the journey


@dataclass(frozen=True)
class Journey:
    """The whole browser path, with every page it saw and every phase it measured."""

    project_id: str
    store: ProjectStore
    #: The pages, as HTML, in the order the reviewer met them.
    script_page: str
    storyboard_page: str
    preview_page: str
    render_page: str
    #: `/projects/{id}/job` right after each wait, exactly as the poll loop sees it.
    script_job: str
    storyboard_job: str
    #: The status codes the forms answered with, keyed by what was posted.
    codes: dict[str, int] = field(default_factory=dict)
    narrations: dict[str, str] = field(default_factory=dict)
    #: Phases: the first full render, then the swap, then the re-render after it.
    rendered: Phase = field(default_factory=Phase)
    swapped: Phase = field(default_factory=Phase)
    rerendered: Phase = field(default_factory=Phase)
    #: `data-preview` on `/preview` before and after the swap.
    preview_state_before: str = ""
    preview_state_after: str = ""
    #: (script, storyboard, preview) approved-or-not, either side of the swap.
    approvals_before: tuple[bool, ...] = ()
    approvals_after: tuple[bool, ...] = ()
    chosen_before: str = ""
    chosen_after: str = ""
    #: FFmpeg calls made *by the choose handler itself* — decision 2 says none.
    choose_encodes: int = 0


def _settle(client: TestClient, project_id: str) -> str:
    """Wait for the worker the way the page's poll loop does, then read `/job` once.

    `JobQueue.wait_idle` is the race-free version of what `hx-trigger="every 1500ms"`
    is doing on screen; sleeping and hoping would make this test flaky on a loaded
    runner and would prove nothing extra.
    """
    assert client.app.state.jobs.wait_idle(JOB_TIMEOUT_S), (
        f"the worker never went idle for {project_id} within {JOB_TIMEOUT_S}s"
    )
    response = client.get(f"/projects/{project_id}/job", headers=_HX)
    assert response.status_code == 200, response.text
    return response.text


def _post(client: TestClient, url: str, **data) -> int:
    """A plain browser form post — no htmx — following nothing, so the 303 is visible."""
    response = client.post(url, data=data or None, follow_redirects=False)
    return response.status_code


def _approvals(store: ProjectStore, project_id: str) -> tuple[bool, ...]:
    approvals = store.load(project_id).approvals
    return (
        approvals.script is not None,
        approvals.storyboard is not None,
        approvals.preview is not None,
    )


@pytest.fixture(scope="module")
def journey(tmp_path_factory) -> Journey:
    """Drive one whole project through the web UI; the tests read what it recorded."""
    tmp_path = tmp_path_factory.mktemp("web_golden_path")

    with pytest.MonkeyPatch.context() as patch:
        # A throwaway user cache, so the response cache starts empty and the first
        # run genuinely calls out (the session fixture redirects it too; this pins
        # it to *this* test's directory).
        patch.setattr(runner_module, "USER_CACHE_DIR", tmp_path / "cache")
        # Distinguishable candidates first, then the counter on top of them.
        patch.setattr(mock_module.MockStock, "download", _variant_download)
        counts = _count_provider_calls(patch)
        calls = _count_ffmpeg_calls(patch)

        settings = Settings(workspace_dir=tmp_path / "workspace")
        app = create_app(settings, providers="mock")
        store: ProjectStore = app.state.store
        codes: dict[str, int] = {}

        with TestClient(app) as client:
            # 1. Create the project from the form on `/`.
            created = client.post(
                "/projects",
                data={
                    "topic": TOPIC,
                    "template": TEMPLATE,
                    "minutes": MINUTES,
                    "voice": VOICE,
                },
                follow_redirects=False,
            )
            codes["create"] = created.status_code
            assert created.status_code == 303, created.text
            project_id = created.headers["location"].rsplit("/", 1)[-1]
            script_job = _settle(client, project_id)

            # 2. Gate 1: read the script, edit every scene, approve.
            script_page = client.get(f"/projects/{project_id}/script").text
            scene_ids = _SCENE.findall(script_page)
            for number, scene_id in enumerate(scene_ids, start=1):
                fields = {"narration": SHORT_NARRATION.format(number=number)}
                if scene_id == SWAPPED_SCENE:
                    fields["query"] = EDITED_QUERY
                codes[f"save:{scene_id}"] = _post(
                    client, f"/projects/{project_id}/scenes/{scene_id}", **fields
                )
            narrations = {
                scene.id: scene.narration for scene in store.load(project_id).scenes
            }
            codes["approve:script"] = _post(
                client, f"/projects/{project_id}/approve/script"
            )

            # 3. Wait for the run the approval started; it stops at gate 2.
            storyboard_job = _settle(client, project_id)

            # 4. Gate 2: look at the shots, approve the storyboard.
            storyboard_page = client.get(f"/projects/{project_id}/storyboard").text
            codes["approve:storyboard"] = _post(
                client, f"/projects/{project_id}/approve/storyboard"
            )
            _settle(client, project_id)

            # 5. Gate 3: build the 480p proxy, watch it, approve, render.
            codes["build:preview"] = _post(client, f"/projects/{project_id}/preview/build")
            _settle(client, project_id)
            preview_page = client.get(f"/projects/{project_id}/preview").text
            preview_state_before = _PREVIEW_STATE.search(preview_page).group(1)
            codes["approve:preview"] = _post(
                client, f"/projects/{project_id}/approve/preview"
            )
            _settle(client, project_id)
            rendered = _observe(store, project_id, calls, counts)
            render_page = client.get(f"/projects/{project_id}/render").text

            approvals_before = _approvals(store, project_id)
            chosen_before = store.load(project_id).scene_by_id(
                SWAPPED_SCENE
            ).visual.chosen.local_path

            # mtime witnesses need a clock tick they can actually resolve.
            time.sleep(0.01)

            # 6. **The DoD.** Back to the storyboard, swap scene two's shot, re-advance.
            calls.clear()
            counts.clear()
            swap = client.post(
                f"/projects/{project_id}/scenes/{SWAPPED_SCENE}/choose",
                data={"candidate_index": SWAPPED_INDEX},
                headers=_HX,
            )
            codes["choose"] = swap.status_code
            assert swap.status_code == 200, swap.text
            choose_encodes = len(calls)
            # Read **before** the advance: `run_pipeline` calls
            # `clear_stale_approvals` itself, so waiting until after the run would
            # make the page's own call unobservable and the assertion vacuous.
            approvals_after = _approvals(store, project_id)
            chosen_after = store.load(project_id).scene_by_id(
                SWAPPED_SCENE
            ).visual.chosen.local_path

            codes["advance"] = _post(client, f"/projects/{project_id}/advance")
            _settle(client, project_id)
            swapped = _observe(store, project_id, calls, counts)

            preview_state_after = _PREVIEW_STATE.search(
                client.get(f"/projects/{project_id}/preview").text
            ).group(1)

            # 7. The swap cleared gate 3; approve it again and re-render.
            calls.clear()
            counts.clear()
            codes["reapprove:preview"] = _post(
                client, f"/projects/{project_id}/approve/preview"
            )
            _settle(client, project_id)
            rerendered = _observe(store, project_id, calls, counts)

        return Journey(
            project_id=project_id,
            store=store,
            script_page=script_page,
            storyboard_page=storyboard_page,
            preview_page=preview_page,
            render_page=render_page,
            script_job=script_job,
            storyboard_job=storyboard_job,
            codes=codes,
            narrations=narrations,
            rendered=rendered,
            swapped=swapped,
            rerendered=rerendered,
            preview_state_before=preview_state_before,
            preview_state_after=preview_state_after,
            approvals_before=approvals_before,
            approvals_after=approvals_after,
            chosen_before=chosen_before,
            chosen_after=chosen_after,
            choose_encodes=choose_encodes,
        )


# ------------------------------------------------------- 1. creating a project


def test_posting_the_form_creates_a_project_and_starts_its_script(journey):
    """The form's 303 is the whole POST-then-redirect contract: no double-create."""
    assert journey.codes["create"] == 303
    assert journey.project_id == PROJECT_ID

    saved = journey.store.load(journey.project_id)
    assert saved.topic == TOPIC
    assert saved.template == TEMPLATE
    # The script job ran on the worker and finished — not blocked, not failed.
    assert _JOB_STATE.search(journey.script_job).group(1) == "done"
    assert saved.scenes, "the script stage produced no scenes"


# ---------------------------------------------------------------- 2. gate one


def test_the_script_page_shows_every_scene_and_saves_edits(journey):
    """Plain form posts, no htmx: the page has to work with JavaScript switched off."""
    shown = _SCENE.findall(journey.script_page)

    assert shown == journey.rendered.scene_ids
    assert SWAPPED_SCENE in shown
    for scene_id in shown:
        assert journey.codes[f"save:{scene_id}"] == 303

    # The edits really landed, and the swapped scene's visual query with them.
    for number, scene_id in enumerate(shown, start=1):
        assert journey.narrations[scene_id] == SHORT_NARRATION.format(number=number)
    assert journey.store.load(journey.project_id).scene_by_id(
        SWAPPED_SCENE
    ).visual.query == EDITED_QUERY


def test_approving_gate_one_runs_the_pipeline_as_far_as_gate_two(journey):
    """The run stops itself at the storyboard gate — `GATE_BEFORE["captions"]`."""
    assert journey.codes["approve:script"] == 303
    assert _JOB_STATE.search(journey.storyboard_job).group(1) == "blocked"
    # The fragment links straight at the screen that clears it.
    assert f'href="/projects/{journey.project_id}/storyboard"' in journey.storyboard_job


# ---------------------------------------------------------------- 3. gate two


def test_the_storyboard_page_offers_a_card_and_a_shot_for_every_scene(journey):
    cards = _SCENE.findall(journey.storyboard_page)

    assert cards == journey.rendered.scene_ids
    project = journey.store.load(journey.project_id)
    for scene in project.scenes:
        assert scene.visual.chosen is not None
        assert len(scene.visual.candidates) > SWAPPED_INDEX, (
            "the swap below needs a second candidate to swap to"
        )


# -------------------------------------------------------- 4. gate three, done


def test_the_browser_alone_took_the_project_to_a_finished_video(journey):
    """**The M2 definition of done: a full project via the browser.**

    Nothing below this line was done from the CLI — every gate was cleared by
    posting the button on its review page.
    """
    assert journey.codes["approve:storyboard"] == 303
    assert journey.codes["build:preview"] == 303
    assert journey.codes["approve:preview"] == 303
    assert journey.approvals_before == (True, True, True)

    final = journey.store.path_for(journey.project_id) / output_relpath(Aspect.WIDE)
    assert final.is_file()
    assert journey.rendered.final_mtime > 0
    assert _RENDER_STATE.search(journey.render_page).group(1) == "ready"
    assert f'src="/media/{journey.project_id}/{output_relpath(Aspect.WIDE)}"' in (
        journey.render_page
    )


def test_the_rendered_video_is_1080p_h264_with_aac_audio(journey):
    video = journey.rendered.streams["video"]
    audio = journey.rendered.streams["audio"]

    assert video["codec_name"] == "h264"
    assert (video["width"], video["height"]) == WIDE_SPEC.size
    assert audio["codec_name"] == "aac"
    assert journey.rendered.expected_duration_s > 0
    assert journey.rendered.duration_s == pytest.approx(
        journey.rendered.expected_duration_s, abs=DURATION_TOLERANCE_S
    )


def test_the_first_pass_really_encoded_every_scene(journey):
    """The premise of every assertion below: there was a finished video to disturb."""
    # More than one scene, or "only that scene" would be the whole video anyway.
    assert len(journey.rendered.scene_ids) >= 3
    assert sorted(journey.rendered.segment_encodes) == sorted(
        segment_relpath(scene_id, Aspect.WIDE) for scene_id in journey.rendered.scene_ids
    )
    assert all(mtime > 0 for mtime in journey.rendered.segments.values())
    assert journey.rendered.preview_mtime > 0
    assert journey.preview_state_before == "ready"


# ------------------------------------------------- 5. the definition of done


def test_swapping_one_shot_at_gate_two_re_encodes_only_that_scene(journey):
    """**The M2 DoD sentence, made executable.**

    One candidate swapped on the storyboard page must re-encode that scene's
    segment, re-join the timeline and rebuild the narration bed — and touch
    nothing else. The encodes are counted at `run_ffmpeg` itself and the segments
    that must not move are witnessed by mtime, so a handler that invalidated the
    whole assemble stage could not pass by producing a correct video slowly.
    """
    assert journey.codes["choose"] == 200  # htmx gets the partial, not a redirect
    assert journey.chosen_after != journey.chosen_before

    # Exactly one segment re-encode, and it is the swapped scene's.
    assert journey.swapped.segment_encodes == [segment_relpath(SWAPPED_SCENE, Aspect.WIDE)]

    # Every other segment is the *same file*, untouched on disk.
    for scene_id, mtime in journey.rendered.segments.items():
        if scene_id == SWAPPED_SCENE:
            assert journey.swapped.segments[scene_id] != mtime
        else:
            assert journey.swapped.segments[scene_id] == mtime

    # The re-run is that segment, the join and the narration bed. Nothing more:
    # the render is behind gate 3, which the swap has just un-approved.
    # Segment, join and narration bed — once per aspect: the Short re-cuts the
    # same swapped shot rather than inheriting the wide segment.
    assert len(journey.swapped.ffmpeg_calls) == 3 * len(ASSEMBLE_ASPECTS)
    assert journey.swapped.final_mtime == journey.rendered.final_mtime


def test_the_swap_spends_nothing_but_the_one_download_it_needs(journey):
    """No re-script, no re-voice, no re-align: the swap touched one field."""
    calls = journey.swapped.provider_calls

    assert calls["llm"] == 0
    assert calls["tts"] == 0
    assert calls["stt"] == 0
    # M1's visuals stage downloads only the hit it chose, so the alternative has
    # to be fetched — once, by the web handler, not by a stage re-running.
    assert calls["stock.download"] == 1


def test_the_choose_handler_runs_no_stage_of_its_own(journey):
    """Design decision 2: handlers enqueue and redirect; they never encode."""
    assert journey.choose_encodes == 0
    assert journey.codes["advance"] == 303


def test_the_swap_clears_gate_three_and_leaves_gate_two_alone(journey):
    """`clear_stale_approvals` decides this; the page must not second-guess it."""
    script, storyboard, preview = journey.approvals_after

    assert (script, storyboard) == (True, True)
    assert preview is False
    # And the reviewer is told why: the proxy they approved is now out of date.
    assert journey.preview_state_after == "stale"


# ------------------------------------------------------ 6. re-approving gate 3


def test_re_approving_gate_three_re_renders_the_final_video_and_nothing_else(journey):
    """The last step of the loop: one shot changed, one video re-rendered."""
    assert journey.codes["reapprove:preview"] == 303

    assert journey.rerendered.segment_encodes == []
    assert len(journey.rerendered.ffmpeg_calls) == len(RENDER_ASPECTS)  # one final encode each
    assert journey.rerendered.provider_calls == Counter()
    assert journey.rerendered.final_mtime != journey.rendered.final_mtime

    video = journey.rerendered.streams["video"]
    assert (video["width"], video["height"]) == WIDE_SPEC.size
    assert journey.rerendered.duration_s == pytest.approx(
        journey.rerendered.expected_duration_s, abs=DURATION_TOLERANCE_S
    )
