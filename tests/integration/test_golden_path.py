"""The M1 definition of done, end to end, through the real CLI and real FFmpeg.

`videomaker new "how ssds work" -t tech_explainer` then `videomaker run <id> --yes`
must produce a playable `output/final_wide.mp4` with burned captions, an immediate
re-run must spend nothing and re-encode nothing, and editing one scene must re-run
that scene alone. Every other test in this suite checks one stage; this one is the
regression that guards the promise as a whole, so it drives the Typer app itself
rather than calling `run_pipeline`, and lets FFmpeg do genuine encodes.

**Why the counters, and not the clock.** "Zero provider calls" and "zero
re-encodes" are counted at the source: every mock provider method and both
`run_ffmpeg` call sites are wrapped, and the per-scene intermediates' mtimes are
compared either side of a run. Wall time on a loaded CI runner is a weak signal, so
the 10 s bound in the spec is asserted as a generous sanity check on top of the
counts, never as the evidence itself.

`--providers mock` keeps the whole run offline and free of the optional `ml` extra,
but the mocks write real media (a genuine 24 kHz wav, the checked-in fixtures), so
FFmpeg is doing the same work here that it does against live providers.

The three runs are done once, in a module-scoped fixture, and each one's
observations are frozen into a `Phase` — the assertions below are then cheap reads
of a moment that has already passed, rather than three more encodes each.
"""

import json
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from typer.testing import CliRunner

from videomaker import runner as runner_module
from videomaker.cli import app
from videomaker.media import ffmpeg as ffmpeg_module
from videomaker.media.ffmpeg import probe_duration, probe_json
from videomaker.models import Aspect, Status
from videomaker.pipeline import assemble as assemble_module
from videomaker.pipeline import render as render_module
from videomaker.pipeline.assemble import WIDE_SPEC, segment_relpath
from videomaker.pipeline.base import SCENE_GAP_S
from videomaker.pipeline.render import output_relpath
from videomaker.project import ProjectStore
from videomaker.providers import mock as mock_module

TOPIC = "how ssds work"
PROJECT_ID = "how-ssds-work"
TEMPLATE = "tech_explainer"
#: The shortest video the template allows (it clamps to four scenes either way), so
#: the real encodes this test performs stay a few seconds rather than a few minutes.
MINUTES = "0.5"

EDITED_SCENE = "s02"
#: A different *word count*, so the edit moves the scene's duration and the segment
#: really is re-encoded — the mock TTS speaks 0.4 s per word.
EDITED_NARRATION = "Electrons sit behind an insulator until a voltage pulls them back out."

#: The spec's re-run promise. Generous on purpose: it is a sanity check on top of
#: the call counts, which are what actually prove nothing was recomputed.
RERUN_BUDGET_S = 10.0
DURATION_TOLERANCE_S = 0.5

#: Every provider method `--providers mock` can reach, labelled by the kind of quota
#: a real provider would have spent making the same call.
PROVIDER_METHODS: tuple[tuple[type, str, str], ...] = (
    (mock_module.MockLLM, "generate", "llm"),
    (mock_module.MockTTS, "synthesize", "tts"),
    (mock_module.MockSTT, "transcribe_words", "stt"),
    (mock_module.MockStock, "search", "stock.search"),
    (mock_module.MockStock, "download", "stock.download"),
    (mock_module.MockImage, "generate_image", "image"),
)


# ------------------------------------------------------------------- observation


@dataclass(frozen=True)
class Phase:
    """Everything one `videomaker run` did, frozen the moment it finished."""

    exit_code: int
    output: str
    seconds: float
    provider_calls: Counter
    #: The argv of every `run_ffmpeg` in stage order; the last item is the output path.
    ffmpeg_calls: list[list[str]] = field(default_factory=list)
    #: scene id -> mtime of `build/<scene>_wide.mp4`, in nanoseconds.
    segments: dict[str, int] = field(default_factory=dict)
    final_mtime: int = 0
    #: ffprobe's view of `output/final_wide.mp4`, and what the project says it should be.
    streams: dict[str, dict] = field(default_factory=dict)
    duration_s: float = 0.0
    expected_duration_s: float = 0.0
    scene_ids: list[str] = field(default_factory=list)

    @property
    def segment_encodes(self) -> list[str]:
        """The output paths of the per-scene encodes only — not the join, bed or render."""
        wanted = {segment_relpath(scene_id, Aspect.WIDE) for scene_id in self.scene_ids}
        return [args[-1] for args in self.ffmpeg_calls if args[-1] in wanted]


def _count_provider_calls(patch: pytest.MonkeyPatch) -> Counter:
    """Tally every mock provider call. A second run must add nothing to this."""
    counts: Counter = Counter()
    for cls, method, label in PROVIDER_METHODS:
        original = getattr(cls, method)

        def counting(*args, _original=original, _label=label, **kwargs):
            counts[_label] += 1
            return _original(*args, **kwargs)

        patch.setattr(cls, method, counting)
    return counts


def _count_ffmpeg_calls(patch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record every real FFmpeg invocation, at both call sites, and still run it."""
    calls: list[list[str]] = []
    original = ffmpeg_module.run_ffmpeg

    def counting(args, **kwargs):
        calls.append(list(args))
        return original(args, **kwargs)

    for module in (assemble_module, render_module):
        patch.setattr(module, "run_ffmpeg", counting)
    return calls


def _expected_duration_s(scenes) -> float:
    """The finished video is every scene's narration plus one gap each."""
    return sum((scene.duration_s or 0.0) + SCENE_GAP_S for scene in scenes)


def _mtime_ns(path: Path) -> int:
    return path.stat().st_mtime_ns if path.is_file() else 0


def _run_phase(
    cli: CliRunner,
    store: ProjectStore,
    counts: Counter,
    ffmpeg_calls: list[list[str]],
) -> Phase:
    """One `videomaker run <id> --providers mock --yes`, with everything it did."""
    counts.clear()
    ffmpeg_calls.clear()
    started = time.monotonic()
    result = cli.invoke(app, ["run", PROJECT_ID, "--providers", "mock", "--yes"])
    seconds = time.monotonic() - started

    root = store.path_for(PROJECT_ID)
    project = store.load(PROJECT_ID)
    final = root / output_relpath(Aspect.WIDE)
    streams: dict[str, dict] = {}
    duration_s = 0.0
    if final.is_file():
        streams = {stream["codec_type"]: stream for stream in probe_json(final)["streams"]}
        duration_s = probe_duration(final)

    return Phase(
        exit_code=result.exit_code,
        output=result.output,
        seconds=seconds,
        provider_calls=counts.copy(),
        ffmpeg_calls=[list(args) for args in ffmpeg_calls],
        segments={
            scene.id: _mtime_ns(root / segment_relpath(scene.id, Aspect.WIDE))
            for scene in project.scenes
        },
        final_mtime=_mtime_ns(final),
        streams=streams,
        duration_s=duration_s,
        expected_duration_s=_expected_duration_s(project.scenes),
        scene_ids=[scene.id for scene in project.scenes],
    )


@dataclass(frozen=True)
class GoldenPath:
    """The whole journey: create, run, re-run, edit one scene and run again."""

    root: Path
    created: object
    first: Phase
    cached: Phase
    edited: Phase


@pytest.fixture(scope="module")
def golden(tmp_path_factory) -> GoldenPath:
    """Drive the CLI through the M1 happy path once; the tests read what it recorded."""
    tmp_path = tmp_path_factory.mktemp("golden_path")
    cli = CliRunner()

    with pytest.MonkeyPatch.context() as patch:
        # A cwd-relative workspace and a throwaway user cache: nothing real is touched,
        # and the response cache starts empty so the first run genuinely calls out.
        patch.chdir(tmp_path)
        patch.setattr(runner_module, "USER_CACHE_DIR", tmp_path / "cache")
        counts = _count_provider_calls(patch)
        ffmpeg_calls = _count_ffmpeg_calls(patch)

        created = cli.invoke(app, ["new", TOPIC, "-t", TEMPLATE, "-m", MINUTES])
        store = ProjectStore(tmp_path / "workspace")

        first = _run_phase(cli, store, counts, ffmpeg_calls)
        cached = _run_phase(cli, store, counts, ffmpeg_calls)

        project = store.load(PROJECT_ID)
        project.scene_by_id(EDITED_SCENE).narration = EDITED_NARRATION
        store.save(project)
        edited = _run_phase(cli, store, counts, ffmpeg_calls)

    return GoldenPath(
        root=store.path_for(PROJECT_ID),
        created=created,
        first=first,
        cached=cached,
        edited=edited,
    )


# ------------------------------------------------------------------- 1. `new`


def test_new_writes_the_project_to_disk(golden):
    assert golden.created.exit_code == 0, golden.created.output
    assert PROJECT_ID in golden.created.output

    saved = json.loads((golden.root / "project.json").read_text())
    assert saved["topic"] == TOPIC
    assert saved["template"] == TEMPLATE


# --------------------------------------------------------- 2. the first run


def test_the_first_run_renders_a_playable_wide_video(golden):
    final = golden.root / output_relpath(Aspect.WIDE)

    assert golden.first.exit_code == 0, golden.first.output
    assert Status.RENDERED.value in golden.first.output
    assert final.is_file()
    assert golden.first.final_mtime > 0


def test_the_rendered_video_is_1080p_h264_with_aac_audio(golden):
    video = golden.first.streams["video"]
    audio = golden.first.streams["audio"]

    assert video["codec_name"] == "h264"
    assert (video["width"], video["height"]) == WIDE_SPEC.size
    assert audio["codec_name"] == "aac"


def test_the_rendered_video_lasts_as_long_as_the_narration(golden):
    assert golden.first.expected_duration_s > 0
    assert golden.first.duration_s == pytest.approx(
        golden.first.expected_duration_s, abs=DURATION_TOLERANCE_S
    )


def test_the_first_run_actually_called_the_providers(golden):
    """Otherwise "zero calls on the second run" would prove nothing at all."""
    calls = golden.first.provider_calls

    assert calls["llm"] >= 1
    assert calls["tts"] == len(golden.first.scene_ids)
    assert calls["stt"] == len(golden.first.scene_ids)
    assert golden.first.segment_encodes == [
        segment_relpath(scene_id, Aspect.WIDE) for scene_id in golden.first.scene_ids
    ]


# ------------------------------------------------------- 3. the cached re-run


def test_a_clean_rerun_spends_no_quota(golden):
    assert golden.cached.exit_code == 0, golden.cached.output
    assert golden.cached.provider_calls == Counter()


def test_a_clean_rerun_runs_ffmpeg_not_even_once(golden):
    assert golden.cached.ffmpeg_calls == []
    assert golden.cached.segments == golden.first.segments
    assert golden.cached.final_mtime == golden.first.final_mtime


def test_a_clean_rerun_finishes_inside_the_budget(golden):
    """A sanity check on the counts above, not the evidence for them."""
    assert golden.cached.seconds < RERUN_BUDGET_S, (
        f"the cached re-run took {golden.cached.seconds:.2f}s"
    )


# --------------------------------------------------- 4. editing a single scene


def test_editing_one_scene_re_voices_and_re_aligns_only_that_scene(golden):
    calls = golden.edited.provider_calls

    assert golden.edited.exit_code == 0, golden.edited.output
    assert calls["tts"] == 1
    assert calls["stt"] == 1
    assert calls["llm"] == 0  # the script is untouched, so nothing is re-written


def test_editing_one_scene_re_encodes_only_that_scene(golden):
    assert golden.edited.segment_encodes == [segment_relpath(EDITED_SCENE, Aspect.WIDE)]

    untouched = [scene_id for scene_id in golden.first.scene_ids if scene_id != EDITED_SCENE]
    assert [golden.edited.segments[scene_id] for scene_id in untouched] == [
        golden.cached.segments[scene_id] for scene_id in untouched
    ]
    assert golden.edited.segments[EDITED_SCENE] != golden.cached.segments[EDITED_SCENE]


def test_editing_one_scene_produces_a_fresh_final_render(golden):
    assert golden.edited.final_mtime != golden.cached.final_mtime
    assert golden.edited.duration_s == pytest.approx(
        golden.edited.expected_duration_s, abs=DURATION_TOLERANCE_S
    )
    # The edit shortened scene two, so the finished video is shorter than it was.
    assert golden.edited.duration_s < golden.first.duration_s
