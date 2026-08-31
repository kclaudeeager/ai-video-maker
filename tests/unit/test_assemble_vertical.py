"""Vertical assembly: every scene is re-cropped from its own source asset.

The milestone's binding constraint is that vertical is **never** a crop of the
finished wide render — a 9:16 frame carved out of the wide output would carry
wide-styled burned-in captions with it. So the vertical graph starts by cutting a
9:16 window out of the *source*, at `crop_focus_x`, before anything is scaled.

That reframe is also what finally makes `crop_focus_x` do something. Until now it
only nudged the pan across a window that was always the full width of the source.

Two properties are load-bearing enough to be asserted directly rather than inferred:

* **The reframe happens in source space, and the motion still scales before it crops.**
  `test_vertical_motion_still_scales_before_it_crops` splits the graph at the reframe
  and re-applies the wide suite's ordering rule to what follows. Cropping the *output*
  window before scaling throws away the pixels the scale needs; cropping the *source*
  window first throws away only pixels no vertical frame could ever show.
* **Segments are cached per aspect.** Re-cropping scene 3 for vertical must not
  invalidate scene 3's wide segment, or every Short would cost a full wide re-encode.
  Proven by counting `run_ffmpeg` calls and witnessing mtimes, the way M1 did.
"""

from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

import pytest

from videomaker.cache import ResponseCache, StageCache
from videomaker.config import Settings
from videomaker.media.ffmpeg import probe_dimensions
from videomaker.models import (
    Aspect,
    AssetRef,
    Motion,
    Project,
    Scene,
    SceneVisual,
    WordTiming,
)
from videomaker.pipeline import assemble as assemble_module
from videomaker.pipeline import base as base_module
from videomaker.pipeline.assemble import (
    PRESCALE_PAN,
    PRESCALE_ZOOM,
    VERTICAL_SPEC,
    WIDE_SPEC,
    build_scene_filter,
    narration_relpath,
    reframe_filter,
    run_assemble,
    scene_hash,
    scene_timeline,
    segment_relpath,
)
from videomaker.pipeline.base import SCENE_GAP_S, StageDeps
from videomaker.pipeline.captions import timeline_word_groups, timeline_words
from videomaker.project import ProjectStore
from videomaker.providers.ratelimit import QuotaTracker

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _asset(duration_s: float | None) -> AssetRef:
    return AssetRef(
        provider="mock",
        source_id="mock-1",
        source_url="https://mock.invalid/1",
        local_path="scenes/s01/asset" + (".mp4" if duration_s else ".jpg"),
        width=1920,
        height=1080,
        duration_s=duration_s,
    )


def _scene(
    *,
    duration: float,
    asset_duration: float | None = None,
    motion: Motion = Motion.PAN,
    sid: str = "s01",
    focus: float = 0.5,
) -> Scene:
    return Scene(
        id=sid,
        narration=f"Narration for {sid}.",
        visual=SceneVisual(
            query=f"{sid} b-roll",
            chosen=_asset(asset_duration),
            motion=motion,
            crop_focus_x=focus,
        ),
        audio_path=f"scenes/{sid}/narration.wav",
        duration_s=duration,
    )


def _still(duration: float = 4.0, **kwargs) -> Scene:
    return _scene(duration=duration, asset_duration=None, **kwargs)


def _video(duration: float = 4.0, asset_duration: float = 8.0, **kwargs) -> Scene:
    return _scene(duration=duration, asset_duration=asset_duration, **kwargs)


def _vertical(scene: Scene) -> str:
    return build_scene_filter(scene, VERTICAL_SPEC, gap_s=SCENE_GAP_S)


# ------------------------------------------------------------ the reframe crop


def test_vertical_cuts_a_nine_by_sixteen_window_out_of_the_source():
    """Spec 4.5's expression, verbatim: the source's own pixels, not the wide render."""
    graph = _vertical(_still())

    assert "crop='min(iw,ih*9/16)':ih:'(iw-ow)*0.500':0" in graph


def test_the_reframe_is_the_first_thing_a_still_does():
    graph = _vertical(_still())

    assert graph.index("crop='min(iw") < graph.index("scale=")


def test_crop_focus_x_moves_the_reframe_window():
    """0.0 is the left edge, 1.0 the right — the field's whole reason to exist."""
    left = _vertical(_still(focus=0.0))
    centre = _vertical(_still(focus=0.5))
    right = _vertical(_still(focus=1.0))

    assert "'(iw-ow)*0.000':0" in left
    assert "'(iw-ow)*0.500':0" in centre
    assert "'(iw-ow)*1.000':0" in right
    assert left != right


def test_an_out_of_range_focus_is_clamped_into_the_frame():
    scene = _still()
    scene.visual.crop_focus_x = 4.0  # past the bound the model itself enforces

    assert "'(iw-ow)*1.000':0" in _vertical(scene)


def test_a_video_asset_is_reframed_after_it_is_trimmed():
    """The trim picks the seconds; the reframe picks the pixels."""
    graph = _vertical(_video())

    assert graph.index("trim=start=") < graph.index("crop='min(iw")


def test_wide_is_never_reframed():
    """M1's wide graphs stay byte-identical; `_cover_scale` already covers 16:9."""
    graph = build_scene_filter(_still(), WIDE_SPEC, gap_s=SCENE_GAP_S)

    assert "min(iw" not in graph
    assert reframe_filter(WIDE_SPEC, 0.5) == []


# ------------------------------------------------------- the rest of the graph


def test_vertical_motion_still_scales_before_it_crops():
    """The wide suite's rule, re-applied to everything after the source reframe."""
    graph = _vertical(_still())
    _, after = graph.split(reframe_filter(VERTICAL_SPEC, 0.5)[0], 1)

    assert "scale=" in after
    assert after.index("scale=") < after.index("crop=")


def test_vertical_pan_prescales_by_the_documented_factor():
    graph = _vertical(_still(motion=Motion.PAN))

    width = round(VERTICAL_SPEC.width * PRESCALE_PAN)
    height = round(VERTICAL_SPEC.height * PRESCALE_PAN)
    assert f"scale={width}:{height}" in graph
    assert f"crop={VERTICAL_SPEC.width}:{VERTICAL_SPEC.height}" in graph


def test_vertical_zoom_prescales_two_times_before_zoompan():
    graph = _vertical(_still(motion=Motion.ZOOM))

    width = round(VERTICAL_SPEC.width * PRESCALE_ZOOM)
    height = round(VERTICAL_SPEC.height * PRESCALE_ZOOM)
    assert f"scale={width}:{height}" in graph
    assert f"s={VERTICAL_SPEC.width}x{VERTICAL_SPEC.height}" in graph


def test_a_vertical_graph_is_not_a_wide_graph():
    scene = _still()

    assert _vertical(scene) != build_scene_filter(scene, WIDE_SPEC, gap_s=SCENE_GAP_S)


def test_the_two_aspects_fingerprint_a_scene_differently():
    """If they hashed alike, one aspect's segment would satisfy the other's cache."""
    scene = _still()
    wide = build_scene_filter(scene, WIDE_SPEC, gap_s=SCENE_GAP_S)

    assert scene_hash(scene, WIDE_SPEC, wide, "digest") != scene_hash(
        scene, VERTICAL_SPEC, _vertical(scene), "digest"
    )


# --------------------------------------------------------------- a real encode


def test_a_real_encode_of_a_wide_clip_is_1080x1920(tmp_path):
    """The only test here that runs FFmpeg: proof the quoted expression parses.

    `min(iw,ih*9/16)` contains a comma, which the filter-graph parser reads as the end
    of the filter unless the argument is quoted. No string assertion can catch that.
    """
    scene = _video(duration=0.5, asset_duration=8.0)
    scene.visual.chosen.local_path = "asset.mp4"
    (tmp_path / "asset.mp4").write_bytes((FIXTURES / "sample_clip.mp4").read_bytes())
    graph = build_scene_filter(scene, VERTICAL_SPEC, gap_s=0.0)

    assemble_module._encode_segment(tmp_path, scene, VERTICAL_SPEC, graph, "out.mp4")

    assert probe_dimensions(tmp_path / "out.mp4") == (1080, 1920)


# ------------------------------------------------------- per-aspect cache scope


def _deps(tmp_path: Path) -> StageDeps:
    settings = Settings(workspace_dir=tmp_path / "workspace")
    return StageDeps(
        settings=settings,
        store=ProjectStore(settings.workspace_dir),
        stage_cache=StageCache(tmp_path / "stages.json"),
        response_cache=ResponseCache(tmp_path / "responses"),
        quota=QuotaTracker(tmp_path / "quota.json"),
    )


def _project(deps: StageDeps) -> Project:
    project = deps.store.create("vertical cache", "tech_explainer", target_minutes=1.0)
    root = deps.store.path_for(project.id)
    scenes = []
    for index in range(1, 4):
        sid = f"s{index:02d}"
        asset, audio = f"scenes/{sid}/asset.jpg", f"scenes/{sid}/narration.wav"
        for relpath in (asset, audio):
            path = root / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"{relpath} bytes".encode())
        scene = _scene(duration=2.0, sid=sid)
        scene.visual.chosen.local_path = asset
        scene.audio_path = audio
        scenes.append(scene)
    project.scenes = scenes
    project.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    deps.store.save(project)
    return project


@pytest.fixture
def recorder(monkeypatch) -> list[list[str]]:
    """Stand in for FFmpeg: record the call, then create the file it promised.

    Creating the output is what lets the test witness mtimes — a segment that was not
    re-encoded still carries the mtime of the run that first wrote it.
    """
    calls: list[list[str]] = []

    def fake_run_ffmpeg(args: list[str], *, cwd: Path, on_progress=None) -> None:
        calls.append(args)
        out = cwd / args[-1]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"encoded %d" % len(calls))

    monkeypatch.setattr(assemble_module, "run_ffmpeg", fake_run_ffmpeg)
    return calls


def _segment_calls(calls: list[list[str]], project: Project, aspect: Aspect) -> list[list[str]]:
    """Just the per-scene segment encodes — `video_wide.mp4` shares their suffix."""
    wanted = {segment_relpath(scene.id, aspect) for scene in project.scenes}
    return [args for args in calls if args[-1] in wanted]


def _mtimes(root: Path, project: Project, aspect: Aspect) -> dict[str, int]:
    return {
        scene.id: (root / segment_relpath(scene.id, aspect)).stat().st_mtime_ns
        for scene in project.scenes
    }


def test_building_vertical_leaves_every_wide_segment_untouched(monkeypatch, tmp_path, recorder):
    """The guard: a Short costs vertical encodes only, never a wide re-encode."""
    deps = _deps(tmp_path)
    project = _project(deps)
    root = deps.store.path_for(project.id)
    run_assemble(project, deps)  # wide alone, as the stage ships today
    before = _mtimes(root, project, Aspect.WIDE)
    recorder.clear()

    monkeypatch.setattr(assemble_module, "ASSEMBLE_ASPECTS", (Aspect.WIDE, Aspect.VERTICAL))
    run_assemble(project, deps)

    assert len(_segment_calls(recorder, project, Aspect.VERTICAL)) == 3
    assert _segment_calls(recorder, project, Aspect.WIDE) == []
    assert _mtimes(root, project, Aspect.WIDE) == before


def test_a_vertical_build_leaves_the_wide_cache_entries_fresh(monkeypatch, tmp_path, recorder):
    """The other half: vertical must not overwrite wide's fingerprints either."""
    deps = _deps(tmp_path)
    project = _project(deps)
    root = deps.store.path_for(project.id)
    monkeypatch.setattr(assemble_module, "ASSEMBLE_ASPECTS", (Aspect.WIDE, Aspect.VERTICAL))
    run_assemble(project, deps)
    before = _mtimes(root, project, Aspect.WIDE)
    recorder.clear()

    monkeypatch.setattr(assemble_module, "ASSEMBLE_ASPECTS", (Aspect.WIDE,))
    result = run_assemble(project, deps)

    assert recorder == []  # nothing at all is re-encoded
    assert result.changed is False
    assert _mtimes(root, project, Aspect.WIDE) == before


def test_the_two_aspects_write_to_different_files():
    assert segment_relpath("s03", Aspect.WIDE) != segment_relpath("s03", Aspect.VERTICAL)


# --------------------------------------------------- the vertical narration bed

#: Durations for the mixed project below. `s02` is deliberately in the *middle* and
#: out of the Short: a bug that computed vertical offsets from the wide scene list
#: would still look perfect on `s01` and be wrong by 3.5 s from `s03` onwards.
MIXED = {
    "s01": (2.0, True),
    "s02": (3.0, False),
    "s03": (1.5, True),
    "s04": (2.5, True),
}
WORDS_PER_SCENE = 4


def _timed_words(sid: str, duration_s: float) -> list[WordTiming]:
    """Zero-gap timings from 0 to `duration_s`, exactly as `align` leaves a scene."""
    step = duration_s / WORDS_PER_SCENE
    return [
        WordTiming(word=f"{sid}w{index}", start_s=index * step, end_s=(index + 1) * step)
        for index in range(WORDS_PER_SCENE)
    ]


def _mixed_project(deps: StageDeps) -> Project:
    """A project whose Short drops a scene from the middle of the long cut."""
    project = deps.store.create("mixed short", "tech_explainer", target_minutes=1.0)
    root = deps.store.path_for(project.id)
    scenes = []
    for sid, (duration, in_short) in MIXED.items():
        asset, audio = f"scenes/{sid}/asset.jpg", f"scenes/{sid}/narration.wav"
        for relpath in (asset, audio):
            path = root / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"{relpath} bytes".encode())
        scene = _scene(duration=duration, sid=sid)
        scene.visual.chosen.local_path = asset
        scene.audio_path = audio
        scene.in_short = in_short
        scene.words = _timed_words(sid, duration)
        scenes.append(scene)
    project.scenes = scenes
    project.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    deps.store.save(project)
    return project


def _narration_call(calls: list[list[str]], aspect: Aspect) -> list[str]:
    """The one FFmpeg invocation that wrote this aspect's narration bed."""
    matches = [args for args in calls if args[-1] == narration_relpath(aspect)]
    assert len(matches) == 1, f"expected one {aspect.value} narration build, got {len(matches)}"
    return matches[0]


def _inputs(args: list[str]) -> list[str]:
    """Every `-i <path>` in an FFmpeg argv, in the order they were passed."""
    return [value for flag, value in pairwise(args) if flag == "-i"]


def _build_both(monkeypatch, deps: StageDeps, project: Project) -> None:
    monkeypatch.setattr(assemble_module, "ASSEMBLE_ASPECTS", (Aspect.WIDE, Aspect.VERTICAL))
    run_assemble(project, deps)


def test_the_vertical_narration_track_uses_only_the_in_short_wavs(
    monkeypatch, tmp_path, recorder
):
    """Spec 4.5: the Short's voice track is the same takes, re-concatenated."""
    deps = _deps(tmp_path)
    project = _mixed_project(deps)

    _build_both(monkeypatch, deps, project)

    assert _inputs(_narration_call(recorder, Aspect.VERTICAL)) == [
        "scenes/s01/narration.wav",
        "scenes/s03/narration.wav",
        "scenes/s04/narration.wav",
    ]


def test_the_wide_narration_track_still_uses_every_scene(monkeypatch, tmp_path, recorder):
    """The long cut is unaffected by `in_short`; only the Short is an edit."""
    deps = _deps(tmp_path)
    project = _mixed_project(deps)

    _build_both(monkeypatch, deps, project)

    assert _inputs(_narration_call(recorder, Aspect.WIDE)) == [
        f"scenes/{sid}/narration.wav" for sid in MIXED
    ]


def test_the_two_narration_beds_are_different_files(monkeypatch, tmp_path, recorder):
    deps = _deps(tmp_path)
    project = _mixed_project(deps)

    _build_both(monkeypatch, deps, project)

    assert narration_relpath(Aspect.WIDE) != narration_relpath(Aspect.VERTICAL)
    root = deps.store.path_for(project.id)
    assert (root / narration_relpath(Aspect.VERTICAL)).is_file()
    assert (root / narration_relpath(Aspect.WIDE)).is_file()


def test_each_vertical_wav_is_padded_to_its_vertical_segment(monkeypatch, tmp_path, recorder):
    """Padded to the *vertical* slot: the wide slot would be the wrong length only
    if the durations differed, so the assertion is on the pad values themselves."""
    deps = _deps(tmp_path)
    project = _mixed_project(deps)

    _build_both(monkeypatch, deps, project)

    args = _narration_call(recorder, Aspect.VERTICAL)
    graph = args[args.index("-filter_complex") + 1]
    segments = scene_timeline(project, gap_s=SCENE_GAP_S, aspect=Aspect.VERTICAL)
    assert [f"apad=whole_dur={segment.duration_s:.3f}" in graph for segment in segments] == [
        True
    ] * len(segments)
    assert graph.count("apad=") == len(segments) == 3


def test_building_the_vertical_narration_calls_no_tts_provider(monkeypatch, tmp_path, recorder):
    """The bed is a re-concatenation, never a re-synthesis. Zero TTS calls."""
    deps = _deps(tmp_path)
    project = _mixed_project(deps)
    synthesized: list[str] = []

    class SpyTTS:
        def synthesize(self, **kwargs) -> None:
            synthesized.append(kwargs.get("text", ""))

        def voices(self) -> list[str]:
            return []

    built: list[tuple[str, str]] = []

    def spy_get_provider(kind: str, name: str, settings):
        built.append((kind, name))
        raise AssertionError(f"assemble built a {kind} provider ({name})")

    monkeypatch.setattr(base_module, "get_provider", spy_get_provider)
    # Pre-seeded so a stage reaching for a *cached* instance is caught too, rather
    # than being let through by `get_provider` never being called.
    deps.instances[("tts", "mock")] = SpyTTS()

    _build_both(monkeypatch, deps, project)

    assert synthesized == []
    assert built == []
    # And the bed really was built, so the zero above is not vacuous.
    assert _inputs(_narration_call(recorder, Aspect.VERTICAL)) != []


# ------------------------------------------- vertical captions on the vertical cut


def test_vertical_caption_offsets_skip_scenes_left_out_of_the_short(tmp_path):
    """Scene N's offset is the sum of the *preceding in_short* scenes plus gaps."""
    deps = _deps(tmp_path)
    project = _mixed_project(deps)

    words = timeline_words(project, Aspect.VERTICAL)

    firsts = {
        words[index * WORDS_PER_SCENE].word[:3]: words[index * WORDS_PER_SCENE].start_s
        for index in range(3)
    }
    assert firsts["s01"] == pytest.approx(0.0)
    # s02 is not in the Short, so s03 follows s01 directly.
    assert firsts["s03"] == pytest.approx(2.0 + SCENE_GAP_S)
    assert firsts["s04"] == pytest.approx(2.0 + SCENE_GAP_S + 1.5 + SCENE_GAP_S)


def test_the_vertical_offsets_differ_from_the_wide_ones(tmp_path):
    """The whole point: a Short's captions cannot be the long cut's captions."""
    deps = _deps(tmp_path)
    project = _mixed_project(deps)

    vertical = timeline_words(project, Aspect.VERTICAL)
    wide = timeline_words(project, Aspect.WIDE)

    assert len(vertical) == 3 * WORDS_PER_SCENE
    assert len(wide) == 4 * WORDS_PER_SCENE
    # s03's first word: 2.5 s into the Short, 6.0 s into the long cut.
    assert vertical[WORDS_PER_SCENE].word == wide[2 * WORDS_PER_SCENE].word == "s03w0"
    assert vertical[WORDS_PER_SCENE].start_s != pytest.approx(wide[2 * WORDS_PER_SCENE].start_s)
    assert wide[2 * WORDS_PER_SCENE].start_s == pytest.approx(2.0 + 3.0 + 2 * SCENE_GAP_S)


def test_every_vertical_caption_scene_starts_where_its_segment_does(tmp_path):
    """The cross-check that catches drift: captions and assembly, offset by offset."""
    deps = _deps(tmp_path)
    project = _mixed_project(deps)

    groups = timeline_word_groups(project, Aspect.VERTICAL)
    segments = scene_timeline(project, gap_s=SCENE_GAP_S, aspect=Aspect.VERTICAL)

    assert len(groups) == len(segments) == 3
    for group, segment in zip(groups, segments, strict=True):
        assert group[0].word.startswith(segment.scene_id)
        assert group[0].start_s == pytest.approx(segment.start_s)


def test_wide_captions_are_unchanged_by_the_new_argument(tmp_path):
    """Wide's `.ass` must stay byte-identical to M1's, so the default is wide."""
    deps = _deps(tmp_path)
    project = _mixed_project(deps)

    assert timeline_words(project) == timeline_words(project, Aspect.WIDE)
    assert timeline_word_groups(project) == timeline_word_groups(project, Aspect.WIDE)
