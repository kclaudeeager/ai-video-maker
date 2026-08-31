"""The `thumbnail` stage: the eighth stage, and the first one added since M1.

Three things are under test, in descending order of how expensive they are to get
wrong.

**1. The trap.** Adding a stage to `STAGE_ORDER` is the change that has bitten this
codebase four times. Ten real projects live in `workspace/projects/`, two of them
fully rendered, and every one of them keeps its work only because the fingerprints
in `cache/stages.json` still match what `STAGE_UNITS` computes. So the fingerprints
of all **seven** pre-existing stages are pinned below as literals captured from the
pre-thumbnail tree — never recomputed with the new code, which is the one edit that
would turn these assertions into tautologies. `script:all` gets its own test on top
of that, because `run_script` replaces `project.scenes` wholesale: staling it does
not merely re-run a stage, it destroys every voiced take, chosen shot and approval
in the project (M3 Task 22 nearly shipped exactly that).

**2. The `required` decision.** The thumbnail unit is `required=True`, and it costs
nothing to make it so — because `thumbnail` is **last** in `STAGE_ORDER`.
`derive_status` returns the status of the last *current* stage, so a project that
rendered before this task landed still derives as `rendered`; it simply also reports
one pending stage, which is the honest answer (it genuinely has no thumbnail) and is
what makes the dashboard offer to build one. Contrast Task 6, where vertical was
inserted into stages that already had a `STATUS_AFTER` below `rendered` and finished
projects visibly dropped. Both halves are asserted.

`required=False` was considered and rejected: `stage_is_current` returns `False` for
a stage with no required units, so a not-required thumbnail would make the stage
*permanently* stale — the guidance panel could never say "finished" again, for any
project, ever. A flag whose only effect is to make a stage unsatisfiable is worse
than no flag.

**3. The picture.** Composition is pure and testable: cover-fit, scrim, auto-fit
type. The one guarantee worth stating numerically is the scrim's — white display
text over an unknown frame is illegible without it, so the scrim is asserted to
carry the text band past WCAG AA (4.5:1) even against a **pure white** source
frame, which is the worst case a photograph can hand us.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image, ImageFont

from videomaker.assets import DISPLAY_FONT, FONTS_DIR
from videomaker.cache import STAGE_ORDER, ResponseCache, StageCache
from videomaker.config import Settings
from videomaker.media.ffmpeg import run_ffmpeg
from videomaker.models import (
    Approvals,
    Aspect,
    AssetRef,
    Motion,
    OutputSpec,
    Project,
    Scene,
    SceneVisual,
    Status,
    WordTiming,
)
from videomaker.pipeline import thumbnail as thumbnail_module
from videomaker.pipeline.assemble import video_relpath
from videomaker.pipeline.base import StageDeps
from videomaker.pipeline.render import fonts_reldir, output_relpath, subtitles_filter
from videomaker.pipeline.thumbnail import (
    ACCENT_COLOUR,
    MAX_FONT_PX,
    MIN_FONT_PX,
    STAGE,
    THUMB_HEIGHT,
    THUMB_WIDTH,
    compose_thumbnail,
    cover_fit,
    display_text,
    fit_headline,
    frame_time_s,
    headline,
    run_thumbnail,
    scrim,
    source_video,
    text_box,
    thumbnail_relpath,
)
from videomaker.project import ProjectStore
from videomaker.providers.ratelimit import QuotaTracker
from videomaker.runner import (
    STAGE_RUNNERS,
    STAGE_UNITS,
    STATUS_AFTER,
    derive_status,
    stage_is_current,
    stamp_stage,
)

# ---------------------------------------------------------------------- the trap

#: Every unit fingerprint of `_project()` as computed by the **pre-thumbnail** tree
#: (M3 Phase C, commit 543475a). Captured by running `STAGE_UNITS` against that
#: code with `git stash`ed work and pinning the result. Never regenerate these from
#: the current tree.
PRE_THUMBNAIL_FINGERPRINTS: dict[str, dict[str, str]] = {
    "script": {"all": "892d619463b359a3"},
    "voice": {
        "s01": "f7303d0d6f28adf2",
        "s02": "c9f26a77af006c4e",
        "s03": "743a2dbd77525c52",
        "s04": "d549feb896763edf",
    },
    "align": {
        "s01": "c835026f420d2661",
        "s02": "7fdd54fde227890c",
        "s03": "21c815eac57c7648",
        "s04": "5f7078b8cbf0df49",
    },
    "visuals": {
        "s01": "79bb4a0cf1c0f30e",
        "s02": "769b4f9d3a5af938",
        "s03": "75903ce83c6061d5",
        "s04": "e785d34c9c4bb554",
    },
    "captions": {"wide": "66c6be2d14ce8fbe", "vertical": "156ad05ff0f48f17"},
    "assemble": {"wide": "cbd71ddf37942068", "vertical": "9df4effd0ac51b10"},
    "render": {"wide": "54d642bffbad1bce", "vertical": "d507375f4384210e"},
}

PRE_THUMBNAIL_STAGES: tuple[str, ...] = tuple(PRE_THUMBNAIL_FINGERPRINTS)


def _scene(sid: str, *, duration: float, in_short: bool = True) -> Scene:
    return Scene(
        id=sid,
        narration=f"Narration for {sid}.",
        visual=SceneVisual(
            query=f"query {sid}",
            chosen=AssetRef(
                provider="mock",
                source_id=f"mock-{sid}",
                source_url=f"https://mock.invalid/{sid}",
                local_path=f"scenes/{sid}/asset.mp4",
                width=1920,
                height=1080,
                duration_s=12.0,
            ),
            motion=Motion.PAN,
            crop_focus_x=0.5,
            trim_start_s=0.0,
        ),
        audio_path=f"scenes/{sid}/narration.wav",
        duration_s=duration,
        words=[
            WordTiming(word=f"{sid}-w{i}", start_s=i * 0.5, end_s=i * 0.5 + 0.5) for i in range(4)
        ],
        in_short=in_short,
    )


def _project() -> Project:
    """The frozen project the pinned hashes were captured from. Do not edit.

    A fully finished one — three approvals, both cuts rendered — because that is the
    shape this task can damage: the two projects in the owner's workspace that have
    a `final_wide.mp4` and a `final_vertical.mp4` on disk.
    """
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return Project(
        id="pinned",
        topic="how ssds work",
        template="tech_explainer",
        created_at=now,
        approvals=Approvals(script=now, storyboard=now, preview=now),
        scenes=[
            _scene("s01", duration=8.0),
            _scene("s02", duration=6.5),
            _scene("s03", duration=9.25, in_short=False),
            _scene("s04", duration=7.0),
        ],
        outputs={
            Aspect.WIDE: OutputSpec(
                aspect=Aspect.WIDE,
                width=1920,
                height=1080,
                scene_ids=["s01", "s02", "s03", "s04"],
                video_path="output/final_wide.mp4",
            ),
            Aspect.VERTICAL: OutputSpec(
                aspect=Aspect.VERTICAL,
                width=1080,
                height=1920,
                scene_ids=["s01", "s02", "s04"],
                video_path="output/final_vertical.mp4",
            ),
        },
    )


def _fingerprints(project: Project, stage: str) -> dict[str, str]:
    return {unit.unit: unit.fingerprint for unit in STAGE_UNITS[stage](project)}


def _thumb_unit(project: Project):
    units = STAGE_UNITS[STAGE](project)
    assert len(units) == 1, "the thumbnail is one deliverable, so it is one unit"
    return units[0]


@pytest.mark.parametrize("stage", PRE_THUMBNAIL_STAGES)
def test_no_pre_existing_unit_hash_moves(stage):
    """Byte-identical to Phase C, or ten real projects restage themselves."""
    assert _fingerprints(_project(), stage) == PRE_THUMBNAIL_FINGERPRINTS[stage]


@pytest.mark.parametrize("stage", PRE_THUMBNAIL_STAGES)
def test_the_new_project_fields_move_no_pre_existing_hash(stage):
    """`thumbnail_text` and `thumbnail_path` are thumbnail-only inputs.

    A project written before this task has neither, and one that gains them must not
    re-run a single upstream unit — least of all `script`.
    """
    project = _project()
    project.thumbnail_text = "A completely different headline"
    project.thumbnail_path = "output/thumbnail.jpg"
    assert _fingerprints(project, stage) == PRE_THUMBNAIL_FINGERPRINTS[stage]


def test_the_script_unit_cannot_be_provoked_into_re_running():
    """The one that destroys work: `run_script` replaces `project.scenes` wholesale.

    Every field this task touches or adds is set here at once; `script:all` must not
    move for any of them.
    """
    project = _project()
    project.thumbnail_text = "Anything at all"
    project.thumbnail_path = "output/thumbnail.jpg"
    for scene in project.scenes:
        scene.in_short = not scene.in_short
    assert _fingerprints(project, "script") == PRE_THUMBNAIL_FINGERPRINTS["script"]


def test_the_captions_render_is_byte_identical_because_no_project_has_a_fonts_dir(tmp_path):
    """M1's `fontsdir` gap is *still* a gap, and closing it here would re-encode.

    `fonts_reldir` looks for `assets/fonts` **inside the project folder**; the faces
    Task 20 bundled live inside the *package*, at `videomaker/assets/fonts`. So the
    filter string, and therefore `render_hash`, is exactly what M1 shipped. Pointing
    `fontsdir` at the package directory would change the filter for every project on
    disk and re-encode every finished video to produce the same pixels.
    """
    (tmp_path / "output").mkdir()
    assert fonts_reldir(tmp_path) == ""
    for aspect in Aspect:
        assert "fontsdir" not in subtitles_filter(tmp_path, aspect)


# ------------------------------------------------------- the stage, and `required`


def test_the_thumbnail_is_the_last_stage_in_the_order():
    """Last, so no finished project's derived status can fall."""
    assert STAGE_ORDER[-1] == STAGE
    assert STAGE_ORDER[:-1] == PRE_THUMBNAIL_STAGES


def test_the_stage_is_wired_into_every_runner_table():
    assert STAGE in STAGE_RUNNERS
    assert STAGE in STAGE_UNITS
    assert STATUS_AFTER[STAGE] is Status.RENDERED


def test_the_thumbnail_unit_is_required():
    """Deliberate: a thumbnail is a deliverable, not a listed extra.

    `required=False` would make `stage_is_current` answer False forever, because it
    returns False when a stage has no required units at all.
    """
    assert _thumb_unit(_project()).required is True


def test_a_rendered_project_without_a_thumbnail_still_derives_as_rendered(tmp_path):
    """The whole point of putting the stage last. Nothing in the workspace moves."""
    project = _project()
    cache = StageCache(tmp_path / "stages.json")
    for stage in PRE_THUMBNAIL_STAGES:
        stamp_stage(project, cache, stage)
    assert project.thumbnail_path is None
    assert derive_status(project, cache) is Status.RENDERED


def test_but_that_project_reports_the_thumbnail_stage_as_pending(tmp_path):
    """...and says so, which is what makes the dashboard offer to build one."""
    project = _project()
    cache = StageCache(tmp_path / "stages.json")
    for stage in PRE_THUMBNAIL_STAGES:
        stamp_stage(project, cache, stage)
    assert stage_is_current(project, cache, STAGE) is False


def test_a_project_with_a_stamped_thumbnail_is_current_all_the_way_through(tmp_path):
    project = _project()
    project.thumbnail_path = thumbnail_relpath()
    cache = StageCache(tmp_path / "stages.json")
    for stage in STAGE_ORDER:
        stamp_stage(project, cache, stage)
    assert all(stage_is_current(project, cache, stage) for stage in STAGE_ORDER)
    assert derive_status(project, cache) is Status.RENDERED


# ------------------------------------------------------- the thumbnail fingerprint


def test_the_thumbnail_is_unproduced_until_the_file_is_recorded():
    assert _thumb_unit(_project()).produced is False
    project = _project()
    project.thumbnail_path = thumbnail_relpath()
    assert _thumb_unit(project).produced is True


def test_choosing_a_different_shot_stales_the_thumbnail():
    """It is a frame of the wide assembly, so it is stale exactly when that is."""
    before = _thumb_unit(_project()).fingerprint
    project = _project()
    project.scene_by_id("s01").visual.chosen.local_path = "scenes/s01/other.mp4"
    assert _thumb_unit(project).fingerprint != before


def test_re_voicing_a_scene_stales_the_thumbnail():
    """A new take moves the timeline, so the frame the thumbnail grabs moves too."""
    before = _thumb_unit(_project()).fingerprint
    project = _project()
    project.scene_by_id("s01").duration_s = 11.0
    assert _thumb_unit(project).fingerprint != before


def test_changing_the_headline_stales_the_thumbnail():
    before = _thumb_unit(_project()).fingerprint
    project = _project()
    project.thumbnail_text = "Solid state, explained"
    assert _thumb_unit(project).fingerprint != before


def test_a_vertical_only_edit_does_not_stale_the_thumbnail():
    """`in_short` re-cuts the Short. The thumbnail is the wide cut's frame."""
    before = _thumb_unit(_project()).fingerprint
    project = _project()
    for scene in project.scenes:
        scene.in_short = not scene.in_short
    assert _thumb_unit(project).fingerprint == before


def test_the_frame_is_taken_from_the_middle_of_the_opening_shot():
    """Deterministic, and never on a cut — a cut frame is a cross-fade of two shots."""
    # s01 runs 8.0 s of narration plus the 0.5 s gap, from zero.
    assert frame_time_s(_project()) == pytest.approx(4.25)


# ------------------------------------------------------------------- the headline


def test_the_headline_is_the_topic_when_nothing_overrides_it():
    assert headline(_project()) == "how ssds work"


def test_the_thumbnail_text_overrides_the_topic():
    project = _project()
    project.thumbnail_text = "  Solid state, explained  "
    assert headline(project) == "Solid state, explained"


def test_a_lowercase_topic_is_set_in_caps():
    """Topics are slug-shaped and lowercase; a display face wants the caps."""
    assert display_text("how ssds work") == "HOW SSDS WORK"


def test_deliberate_casing_is_left_alone():
    """If the author typed a capital, they chose it — do not shout over them."""
    assert display_text("How SSDs work") == "How SSDs work"


# ------------------------------------------------------------------ the type fit


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(DISPLAY_FONT), size)


@pytest.mark.parametrize(
    "text",
    [
        "GO",
        "HOW SSDS WORK",
        "WHY YOUR DOCUMENT FALLS APART EVERY TIME YOU EDIT IT",
        (
            "A headline so long that no sane person would ever type it into the box, "
            "going on and on well past the point where any font size could hold it "
            "inside seven hundred and twenty lines of picture"
        ),
        "SUPERCALIFRAGILISTICEXPIALIDOCIOUSLYUNBREAKABLEWORD",
    ],
)
def test_every_fitted_line_stays_inside_the_text_box(text):
    left, top, right, bottom = text_box((THUMB_WIDTH, THUMB_HEIGHT))
    size, lines = fit_headline(text, right - left, bottom - top)
    assert lines, "something must be drawn"
    assert MIN_FONT_PX <= size <= MAX_FONT_PX
    font = _font(size)
    for line in lines:
        assert font.getlength(line) <= right - left
    assert len(lines) * font.size <= bottom - top


def test_a_long_headline_gets_a_smaller_face_than_a_short_one():
    left, top, right, bottom = text_box((THUMB_WIDTH, THUMB_HEIGHT))
    short, _ = fit_headline("GO", right - left, bottom - top)
    long, _ = fit_headline(
        "WHY YOUR DOCUMENT FALLS APART EVERY TIME YOU EDIT IT", right - left, bottom - top
    )
    assert short > long


def test_a_short_headline_takes_the_largest_face_available():
    left, top, right, bottom = text_box((THUMB_WIDTH, THUMB_HEIGHT))
    size, lines = fit_headline("GO", right - left, bottom - top)
    assert size == MAX_FONT_PX
    assert lines == ["GO"]


def test_the_headline_wraps_on_words_rather_than_overflowing():
    left, top, right, bottom = text_box((THUMB_WIDTH, THUMB_HEIGHT))
    _, lines = fit_headline(
        "WHY YOUR DOCUMENT FALLS APART EVERY TIME YOU EDIT IT", right - left, bottom - top
    )
    assert len(lines) > 1
    assert " ".join(lines).replace("…", "") in "WHY YOUR DOCUMENT FALLS APART EVERY TIME YOU EDIT IT"


# ---------------------------------------------------------------------- the scrim


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    channels = []
    for value in rgb[:3]:
        srgb = value / 255
        channels.append(srgb / 12.92 if srgb <= 0.04045 else ((srgb + 0.055) / 1.055) ** 2.4)
    red, green, blue = channels
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    first, second = sorted((_relative_luminance(a), _relative_luminance(b)), reverse=True)
    return (first + 0.05) / (second + 0.05)


def _colours(image: Image.Image) -> list[tuple[int, int, int]]:
    """Every distinct RGB value in `image`. `getcolors` rather than `getdata`, which
    Pillow 14 removes."""
    counted = image.convert("RGB").getcolors(maxcolors=1 << 24)
    assert counted is not None
    return [colour for _count, colour in counted]


def _brightest(image: Image.Image) -> tuple[int, int, int]:
    return max(_colours(image), key=_relative_luminance)


def test_the_scrim_is_transparent_at_the_top_and_opaque_at_the_bottom():
    overlay = scrim((THUMB_WIDTH, THUMB_HEIGHT))
    assert overlay.size == (THUMB_WIDTH, THUMB_HEIGHT)
    alphas = [overlay.getpixel((0, y))[3] for y in range(THUMB_HEIGHT)]
    assert alphas[0] == 0
    assert alphas[-1] > 200
    assert alphas == sorted(alphas), "the ramp must never brighten on the way down"


def test_the_scrim_carries_white_text_past_wcag_aa_over_a_white_frame(tmp_path):
    """The claim the scrim exists to make, measured against the worst case.

    A blown-out sky behind white display text is 1:1 — invisible. The scrim has to
    do better than "looks darker": every pixel of the text box must clear 4.5:1
    against the white the headline is drawn in.
    """
    white = Image.new("RGB", (THUMB_WIDTH, THUMB_HEIGHT), (255, 255, 255))
    assert _contrast((255, 255, 255), (255, 255, 255)) == pytest.approx(1.0)

    scrimmed = Image.alpha_composite(white.convert("RGBA"), scrim(white.size)).convert("RGB")
    left, top, right, bottom = text_box(white.size)
    band = scrimmed.crop((left, top, right, bottom))
    assert _contrast(_brightest(band), (255, 255, 255)) >= 4.5


@pytest.mark.parametrize("text_top", [316, 420, 520, 620])
def test_the_guarantee_holds_wherever_the_headline_lands(text_top):
    """The composer moves the ramp with the type, so the promise moves with it.

    A one-line headline sits near the bottom and gets a shallower scrim; the alpha it
    is given at its own first row must still be the one the contrast claim was sized
    from, or short headlines would be the illegible case.
    """
    white = Image.new("RGBA", (THUMB_WIDTH, THUMB_HEIGHT), (255, 255, 255, 255))
    scrimmed = Image.alpha_composite(white, scrim(white.size, text_top=text_top))
    band = scrimmed.convert("RGB").crop((0, text_top, THUMB_WIDTH, THUMB_HEIGHT))
    assert _contrast(_brightest(band), (255, 255, 255)) >= 4.5


def test_the_accent_rule_reads_against_the_scrim():
    """The one non-white mark on the picture still has to be visible."""
    white = Image.new("RGBA", (THUMB_WIDTH, THUMB_HEIGHT), (255, 255, 255, 255))
    scrimmed = Image.alpha_composite(white, scrim(white.size)).convert("RGB")
    _, top, _, bottom = text_box(white.size)
    ground = scrimmed.getpixel((0, (top + bottom) // 2))
    assert _contrast(ACCENT_COLOUR, ground) >= 3.0


# ---------------------------------------------------------------- the composition


@pytest.mark.parametrize("source_size", [(400, 1200), (2000, 500), (1280, 720)])
def test_cover_fit_fills_the_frame_without_letterboxing(source_size):
    plain = Image.new("RGB", source_size, (10, 200, 10))
    fitted = cover_fit(plain, (THUMB_WIDTH, THUMB_HEIGHT))
    assert fitted.size == (THUMB_WIDTH, THUMB_HEIGHT)
    # One colour and one colour only: any padding, on any edge, shows up as black.
    assert _colours(fitted) == [(10, 200, 10)]


def test_cover_fit_scales_evenly_rather_than_stretching():
    """The other half of "cover": a square in the source is still a square.

    A fit that scaled the axes independently would also fill the frame and pass the
    letterbox test above, while distorting every face in the shot.
    """
    tall = Image.new("RGB", (400, 1200), (0, 0, 0))
    tall.paste((255, 255, 255), (150, 550, 250, 650))  # a 100x100 square

    fitted = cover_fit(tall, (THUMB_WIDTH, THUMB_HEIGHT))

    left, top, right, bottom = fitted.convert("L").point(lambda v: 255 if v > 128 else 0).getbbox()
    scale = THUMB_WIDTH / 400  # the source is far too narrow, so width drives the fit
    assert right - left == pytest.approx(100 * scale, abs=2)
    assert bottom - top == pytest.approx(100 * scale, abs=2)


def test_the_composed_thumbnail_is_a_720p_rgb_picture():
    frame = Image.new("RGB", (1920, 1080), (120, 130, 140))
    out = compose_thumbnail(frame, "HOW SSDS WORK")
    assert out.size == (THUMB_WIDTH, THUMB_HEIGHT)
    assert out.mode == "RGB"


def test_the_composition_darkens_the_bottom_and_leaves_the_top_alone():
    """Measured with no headline, so the glyphs cannot stand in for the scrim."""
    frame = Image.new("RGB", (1920, 1080), (200, 200, 200))
    out = compose_thumbnail(frame, "")
    assert _colours(out.crop((0, 0, THUMB_WIDTH, 40))) == [(200, 200, 200)]
    left, top, right, bottom = text_box(out.size)
    band = out.crop((left, top, right, bottom))
    assert _relative_luminance(_brightest(band)) < _relative_luminance((200, 200, 200))


def test_the_headline_is_actually_drawn():
    """White glyph pixels appear in the text box, and only there."""
    frame = Image.new("RGB", (1920, 1080), (0, 0, 0))
    out = compose_thumbnail(frame, "HOW SSDS WORK")
    left, top, right, bottom = text_box(out.size)
    assert (255, 255, 255) in _colours(out.crop((left, top, right, bottom)))
    assert (255, 255, 255) not in _colours(out.crop((0, 0, THUMB_WIDTH, top)))


def test_an_empty_headline_still_produces_a_picture():
    """A thumbnail with no words beats a stage that raises on an empty override."""
    frame = Image.new("RGB", (1920, 1080), (30, 40, 50))
    out = compose_thumbnail(frame, "")
    assert out.size == (THUMB_WIDTH, THUMB_HEIGHT)


# ----------------------------------------------------------------------- the font


def test_the_display_face_is_the_one_task_20_bundled():
    """No second family. `DISPLAY_FONT` is Space Grotesk Bold, already in the wheel."""
    assert thumbnail_module.FONT_PATH == DISPLAY_FONT
    assert DISPLAY_FONT.parent == FONTS_DIR
    assert DISPLAY_FONT.name == "SpaceGrotesk-Bold.ttf"
    assert DISPLAY_FONT.is_file()


def test_no_font_family_was_added_for_the_thumbnail():
    families = {path.name.split("-")[0] for path in FONTS_DIR.iterdir() if path.suffix != ".txt"}
    assert families == {"SpaceGrotesk", "CharisSIL"}


# ------------------------------------------------------------------ the stage run


def _deps(tmp_path: Path) -> StageDeps:
    settings = Settings(workspace_dir=tmp_path / "workspace")
    return StageDeps(
        settings=settings,
        store=ProjectStore(settings.workspace_dir),
        stage_cache=StageCache(tmp_path / "stages.json"),
        response_cache=ResponseCache(tmp_path / "responses"),
        quota=QuotaTracker(tmp_path / "quota.json"),
    )


def _rendered_project(deps: StageDeps) -> Project:
    """A project with a real, playable `build/video_wide.mp4` and a wide output."""
    project = deps.store.create("how ssds work", "tech_explainer", target_minutes=0.5)
    project.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    project.scenes = [_scene("s01", duration=2.0), _scene("s02", duration=2.0)]
    project.outputs = {
        Aspect.WIDE: OutputSpec(
            aspect=Aspect.WIDE,
            width=1920,
            height=1080,
            scene_ids=["s01", "s02"],
            video_path=output_relpath(Aspect.WIDE),
        )
    }
    root = deps.store.path_for(project.id)
    (root / "build").mkdir(parents=True, exist_ok=True)
    run_ffmpeg(
        [
            "-f", "lavfi",
            "-i", "testsrc=s=1920x1080:r=30:d=6",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", "6",
            video_relpath(Aspect.WIDE),
        ],
        cwd=root,
    )
    deps.store.save(project)
    return project


def test_the_stage_writes_a_1280x720_jpeg_and_records_it(tmp_path):
    deps = _deps(tmp_path)
    project = _rendered_project(deps)

    result = run_thumbnail(project, deps)

    assert result.changed is True
    path = deps.store.path_for(project.id) / thumbnail_relpath()
    assert path.is_file()
    with Image.open(path) as image:
        assert image.size == (THUMB_WIDTH, THUMB_HEIGHT)
        assert image.format == "JPEG"
    assert project.thumbnail_path == thumbnail_relpath()


def test_a_second_run_redraws_nothing(tmp_path):
    deps = _deps(tmp_path)
    project = _rendered_project(deps)
    run_thumbnail(project, deps)
    path = deps.store.path_for(project.id) / thumbnail_relpath()
    before = path.stat().st_mtime_ns

    result = run_thumbnail(project, deps)

    assert result.changed is False
    assert result.skipped_units == 1
    assert path.stat().st_mtime_ns == before


def test_changing_the_headline_redraws(tmp_path):
    deps = _deps(tmp_path)
    project = _rendered_project(deps)
    run_thumbnail(project, deps)
    path = deps.store.path_for(project.id) / thumbnail_relpath()
    before = path.read_bytes()

    project.thumbnail_text = "Solid state, explained"
    assert run_thumbnail(project, deps).changed is True

    assert path.read_bytes() != before


def test_a_project_with_nothing_assembled_is_skipped_rather_than_failed(tmp_path):
    """The runner reaches `thumbnail` on every run, gates included — it must no-op."""
    deps = _deps(tmp_path)
    project = deps.store.create("nothing yet", "tech_explainer")
    deps.store.save(project)

    result = run_thumbnail(project, deps)

    assert result.changed is False
    assert result.skipped_units == 1
    assert project.thumbnail_path is None


def test_the_frame_comes_from_the_uncaptioned_assembly_not_the_burned_render(tmp_path):
    """Otherwise the thumbnail carries a line of burned-in subtitles across it.

    `build/video_wide.mp4` is the same picture without the `.ass` burned in, so it is
    preferred; the final render is only the fallback for a project whose `build/` has
    been cleaned away.
    """
    deps = _deps(tmp_path)
    project = _rendered_project(deps)
    root = deps.store.path_for(project.id)

    # Both on disk, which is the state of every rendered project: the assembly wins.
    (root / "output").mkdir(parents=True, exist_ok=True)
    (root / output_relpath(Aspect.WIDE)).write_bytes(b"burned-in captions live here")
    assert source_video(root) == root / video_relpath(Aspect.WIDE)

    # ...and only a cleaned `build/` falls back to the deliverable.
    (root / video_relpath(Aspect.WIDE)).unlink()
    assert source_video(root) == root / output_relpath(Aspect.WIDE)

    # Nothing at all is neither, and `run_thumbnail` must survive it.
    (root / output_relpath(Aspect.WIDE)).unlink()
    assert source_video(root) is None
