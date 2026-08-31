"""The Short, over **real** FFmpeg: 1080x1920, the `in_short` subset, vertical type.

The wide integration test proves the tail of the pipeline runs. This one proves the
three things that make the second aspect a *Short* rather than a second copy of the
long video, and each of them has a plausible bug that ffprobe cannot see:

* **The cut is the `in_short` subset.** `s02` sits in the middle of the project and
  out of the Short on purpose: a vertical timeline computed from the wide scene list
  would still look right on `s01` and be wrong from `s03` onwards.
* **The captions are the vertical style, not the wide one scaled.** Read out of the
  written `.ass` (96 px type, three words a chunk, a 1080x1920 play resolution) *and*
  out of the burned-in pixels, because a `subtitles=` filter that picked up the wrong
  style file still produces a perfectly playable mp4.
* **The three-minute rule refuses rather than truncates.** An over-long Short and an
  empty one are both errors, and neither one may quietly rewrite `final_vertical.mp4`.

Six real encodes, so the narrations are deliberately tiny and the fixture is shared.
"""

import subprocess

import pytest

from videomaker.cache import ResponseCache, StageCache
from videomaker.config import Settings
from videomaker.media.ass import STYLES
from videomaker.media.ffmpeg import probe_duration, probe_json
from videomaker.models import Aspect, Scene, SceneVisual, VisualKind
from videomaker.pipeline.align import run_align
from videomaker.pipeline.assemble import (
    MAX_SHORT_S,
    VERTICAL_SPEC,
    WIDE_SPEC,
    narration_relpath,
    run_assemble,
    segment_relpath,
    short_duration_s,
    timeline_duration_s,
    video_relpath,
)
from videomaker.pipeline.base import SCENE_GAP_S, StageDeps
from videomaker.pipeline.captions import caption_relpath, run_captions
from videomaker.pipeline.render import (
    ShortNotRenderable,
    output_relpath,
    run_render,
)
from videomaker.pipeline.visuals import run_visuals
from videomaker.pipeline.voice import run_voice
from videomaker.project import ProjectStore
from videomaker.providers.ratelimit import QuotaTracker

#: Six words a scene: enough that a five-word wide chunk and a three-word vertical
#: chunk are visibly different, short enough that six real encodes stay quick.
#: `s02` is out of the Short and in the *middle* of the long cut, which is the only
#: arrangement that catches a vertical timeline built from the wide scene list.
SCENES = (
    ("s01", "Drives store bytes on tiny cells", VisualKind.STOCK_PHOTO, True),
    ("s02", "Controllers shuffle pages behind the scenes", VisualKind.STOCK_PHOTO, False),
    ("s03", "Electrons hide beneath a thin insulator", VisualKind.STOCK_VIDEO, True),
)
SHORT_SCENE_IDS = [sid for sid, _, _, in_short in SCENES if in_short]

SECONDS_PER_WORD = 0.4  # the mock TTS rate
DURATION_TOLERANCE_S = 0.3

#: Any pixel this bright in the flat slate-grey photo fixture is caption text.
BRIGHT = 240

#: Where the vertical caption line is designed to sit: `margin_v` 672 under a 96 px
#: face on a 1920-high frame puts the line's centre at 62 % of the frame. Asserted
#: with room for the font's own metrics, but nowhere near the 90 % a wide-styled
#: caption (`margin_v` 160) would land at in the same frame.
CAPTION_CENTRE_FRACTION = 0.62
CAPTION_CENTRE_TOLERANCE_PX = 70
#: A 64 px wide caption's ink is roughly 50 px tall; a 96 px one is well past this.
MIN_CAPTION_INK_PX = 62
#: No caption ink may reach the bottom eighth — that is where the *wide* style lands.
WIDE_STYLE_BAND_TOP_FRACTION = 0.80


def _deps(tmp_path):
    settings = Settings(
        workspace_dir=tmp_path / "workspace",
        provider_chains={
            "tts": ["mock"],
            "stt": ["mock"],
            "stock": ["mock"],
            "image": ["mock"],
        },
    )
    return StageDeps(
        settings=settings,
        store=ProjectStore(settings.workspace_dir),
        stage_cache=StageCache(tmp_path / "stages.json"),
        response_cache=ResponseCache(tmp_path / "responses"),
        quota=QuotaTracker(tmp_path / "quota.json"),
    )


def _project(deps):
    project = deps.store.create("how ssds work", "tech_explainer", target_minutes=1.0)
    project.scenes = [
        Scene(
            id=sid,
            narration=narration,
            visual=SceneVisual(query=f"{sid} b-roll", kind=kind),
            in_short=in_short,
        )
        for sid, narration, kind, in_short in SCENES
    ]
    deps.store.save(project)
    return project


def _expected_duration(*, only_short: bool) -> float:
    return sum(
        len(narration.split()) * SECONDS_PER_WORD + SCENE_GAP_S
        for _, narration, _, in_short in SCENES
        if in_short or not only_short
    )


def _frame_rows(path, at_s, spec):
    """The rows of one decoded frame that contain any near-white pixel.

    Pixels, not metadata: ffprobe cannot tell a video with burned-in subtitles from
    the same video without them, and it certainly cannot tell which *style* was used.
    """
    raw = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-nostdin",
            "-ss", str(at_s),
            "-i", str(path),
            "-frames:v", "1",
            "-vf", "format=gray",
            "-f", "rawvideo", "-",
        ],
        capture_output=True,
        check=True,
    ).stdout
    assert len(raw) == spec.width * spec.height, f"no {spec.width}x{spec.height} frame at {at_s}s"
    return [
        row
        for row in range(spec.height)
        if any(value >= BRIGHT for value in raw[row * spec.width : (row + 1) * spec.width])
    ]


#: Index of the free-text field in an ASS `Dialogue:` line — the nine before it are
#: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV and Effect.
DIALOGUE_TEXT_FIELD = 9


def _dialogue_texts(text: str) -> list[str]:
    return [
        line.removeprefix("Dialogue: ").split(",", DIALOGUE_TEXT_FIELD)[DIALOGUE_TEXT_FIELD]
        for line in text.splitlines()
        if line.startswith("Dialogue:")
    ]


def _style_fields(text: str) -> list[str]:
    line = next(line for line in text.splitlines() if line.startswith("Style: "))
    return line.removeprefix("Style: ").split(",")


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    """One full run of both aspects, shared by the read-only assertions below."""
    tmp_path = tmp_path_factory.mktemp("render_vertical")
    deps = _deps(tmp_path)
    project = _project(deps)
    run_voice(project, deps)
    run_align(project, deps)
    run_visuals(project, deps)
    run_captions(project, deps)
    run_assemble(project, deps)
    result = run_render(project, deps)
    return deps, project, result


# ------------------------------------------------------------------- the Short itself


def test_render_produces_a_playable_vertical_video(rendered):
    deps, project, result = rendered
    path = deps.store.path_for(project.id) / output_relpath(Aspect.VERTICAL)

    assert result.changed is True
    assert path.is_file()

    info = probe_json(path)
    streams = {stream["codec_type"]: stream for stream in info["streams"]}
    assert streams["video"]["codec_name"] == "h264"
    assert (streams["video"]["width"], streams["video"]["height"]) == (1080, 1920)
    assert (streams["video"]["width"], streams["video"]["height"]) == VERTICAL_SPEC.size
    assert streams["video"]["r_frame_rate"] == f"{VERTICAL_SPEC.fps}/1"
    assert streams["audio"]["codec_name"] == "aac"


def test_the_short_runs_the_in_short_timeline_not_the_whole_video(rendered):
    """The duration is the Short's own cut — and demonstrably shorter than wide's."""
    deps, project, _ = rendered
    root = deps.store.path_for(project.id)

    vertical = probe_duration(root / output_relpath(Aspect.VERTICAL))
    wide = probe_duration(root / output_relpath(Aspect.WIDE))

    assert vertical == pytest.approx(_expected_duration(only_short=True), abs=DURATION_TOLERANCE_S)
    assert vertical == pytest.approx(short_duration_s(project), abs=DURATION_TOLERANCE_S)
    assert wide == pytest.approx(_expected_duration(only_short=False), abs=DURATION_TOLERANCE_S)
    assert vertical < wide


def test_the_short_is_built_from_the_in_short_scenes_only(rendered):
    deps, project, _ = rendered
    root = deps.store.path_for(project.id)
    spec = project.outputs[Aspect.VERTICAL]

    assert spec.scene_ids == SHORT_SCENE_IDS
    assert spec.video_path == output_relpath(Aspect.VERTICAL)
    assert (spec.width, spec.height) == VERTICAL_SPEC.size
    # ...and it survived to disk, not just in memory.
    assert deps.store.load(project.id).outputs[Aspect.VERTICAL].video_path == spec.video_path
    # The dropped scene has a wide segment and no vertical one.
    assert (root / segment_relpath("s02", Aspect.WIDE)).is_file()
    assert not (root / segment_relpath("s02", Aspect.VERTICAL)).is_file()
    assert (root / narration_relpath(Aspect.VERTICAL)).is_file()


def test_the_wide_video_is_untouched_by_the_short(rendered):
    """Adding the Short must not re-frame or re-cut the long video."""
    deps, project, _ = rendered
    root = deps.store.path_for(project.id)

    info = probe_json(root / output_relpath(Aspect.WIDE))
    video = next(s for s in info["streams"] if s["codec_type"] == "video")

    assert (video["width"], video["height"]) == WIDE_SPEC.size
    assert project.outputs[Aspect.WIDE].scene_ids == [sid for sid, _, _, _ in SCENES]
    assert timeline_duration_s(project, Aspect.WIDE) == pytest.approx(
        _expected_duration(only_short=False)
    )


# --------------------------------------------------------------- the vertical captions


def test_the_vertical_ass_is_authored_for_a_1080x1920_frame(rendered):
    deps, project, _ = rendered
    text = (deps.store.path_for(project.id) / caption_relpath(Aspect.VERTICAL)).read_text()

    assert "PlayResX: 1080" in text
    assert "PlayResY: 1920" in text
    assert "PlayResX: 1920" not in text  # the wide frame, had the aspect been ignored


def test_the_vertical_ass_uses_the_vertical_style_and_not_the_wide_one(rendered):
    deps, project, _ = rendered
    root = deps.store.path_for(project.id)
    fields = _style_fields((root / caption_relpath(Aspect.VERTICAL)).read_text())
    wide = _style_fields((root / caption_relpath(Aspect.WIDE)).read_text())

    assert fields[2] == "96"  # Fontsize
    assert fields[21] == "672"  # MarginV
    assert fields[2] == str(STYLES[Aspect.VERTICAL].font_size)
    assert wide[2] == str(STYLES[Aspect.WIDE].font_size) == "64"
    assert fields[2] != wide[2]
    assert fields[21] != wide[21]


def test_the_vertical_captions_are_chunked_three_words_at_a_time(rendered):
    deps, project, _ = rendered
    root = deps.store.path_for(project.id)
    vertical = [len(text.split()) for text in _dialogue_texts(
        (root / caption_relpath(Aspect.VERTICAL)).read_text()
    )]
    wide = [len(text.split()) for text in _dialogue_texts(
        (root / caption_relpath(Aspect.WIDE)).read_text()
    )]

    assert vertical, "the vertical .ass carries no dialogue at all"
    assert max(vertical) == STYLES[Aspect.VERTICAL].words_per_chunk == 3
    assert max(wide) == STYLES[Aspect.WIDE].words_per_chunk == 5


def test_the_vertical_captions_carry_only_the_in_short_words(rendered):
    """The offsets come from the Short's own timeline, not the long cut's."""
    deps, project, _ = rendered
    root = deps.store.path_for(project.id)
    text = (root / caption_relpath(Aspect.VERTICAL)).read_text()

    assert "Controllers" not in text  # `s02` is out of the Short
    assert "Electrons" in text
    assert "Controllers" in (root / caption_relpath(Aspect.WIDE)).read_text()


def test_the_vertical_captions_are_burned_at_the_vertical_band(rendered):
    """Pixels: a caption at 90 % of the frame is the wide layout in a 9:16 box."""
    deps, project, _ = rendered
    root = deps.store.path_for(project.id)
    at_s = 0.6  # inside `s01`'s narration, over the flat-grey photo fixture

    before = _frame_rows(root / video_relpath(Aspect.VERTICAL), at_s, VERTICAL_SPEC)
    rows = _frame_rows(root / output_relpath(Aspect.VERTICAL), at_s, VERTICAL_SPEC)

    assert before == []  # the fixture is flat grey; every bright row below is text
    assert rows, "no burned-in caption in the Short"
    centre = (rows[0] + rows[-1]) / 2
    assert centre == pytest.approx(
        CAPTION_CENTRE_FRACTION * VERTICAL_SPEC.height, abs=CAPTION_CENTRE_TOLERANCE_PX
    )
    assert rows[-1] - rows[0] + 1 >= MIN_CAPTION_INK_PX  # 96 px type, not 64 px
    assert rows[-1] < WIDE_STYLE_BAND_TOP_FRACTION * VERTICAL_SPEC.height


# ------------------------------------------------------------- the three-minute rule


def test_an_over_long_short_is_refused_and_the_file_is_left_alone(rendered):
    """Refuse, never truncate: a Short chopped at 180 s ends mid-sentence."""
    deps, project, _ = rendered
    root = deps.store.path_for(project.id)
    before = (root / output_relpath(Aspect.VERTICAL)).stat().st_mtime_ns
    over = project.model_copy(deep=True)
    over.scene_by_id("s01").duration_s = MAX_SHORT_S + 60.0

    with pytest.raises(ShortNotRenderable) as excinfo:
        run_render(over, deps)

    message = str(excinfo.value)
    assert "s01" in message
    assert "180" in message
    assert (root / output_relpath(Aspect.VERTICAL)).stat().st_mtime_ns == before


def test_an_empty_short_is_refused(rendered):
    deps, project, _ = rendered
    root = deps.store.path_for(project.id)
    before = (root / output_relpath(Aspect.VERTICAL)).stat().st_mtime_ns
    empty = project.model_copy(deep=True)
    for scene in empty.scenes:
        scene.in_short = False

    with pytest.raises(ShortNotRenderable, match="nothing is marked for the Short"):
        run_render(empty, deps)

    assert (root / output_relpath(Aspect.VERTICAL)).stat().st_mtime_ns == before


# -------------------------------------------------------------------------- the cache


def test_a_clean_rerun_re_encodes_neither_aspect(rendered):
    deps, project, _ = rendered
    root = deps.store.path_for(project.id)
    before = {
        aspect: (root / output_relpath(aspect)).stat().st_mtime_ns
        for aspect in (Aspect.WIDE, Aspect.VERTICAL)
    }

    result = run_render(project, deps)

    assert result.changed is False
    assert result.skipped_units == 2  # one per aspect
    assert {
        aspect: (root / output_relpath(aspect)).stat().st_mtime_ns
        for aspect in (Aspect.WIDE, Aspect.VERTICAL)
    } == before
