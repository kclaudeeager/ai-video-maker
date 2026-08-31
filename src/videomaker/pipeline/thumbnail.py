"""The thumbnail stage: one 1280x720 JPEG per project, drawn over a real frame.

The eighth stage, and the first added since M1. Everything else about it is
ordinary — a cache key, a fingerprint, a `StageResult` — so this docstring is about
the three decisions that were not.

**Why it is last in `STAGE_ORDER`.** `derive_status` returns the status of the last
*current* stage, so a stage appended after `render` cannot lower any project's
status: the two finished projects in the owner's workspace still derive as
`rendered`, they simply also report one stage pending. That is what makes the unit
`required=True` cost nothing, and `required=True` is what a deliverable deserves —
a not-required unit would make `stage_is_current` answer `False` forever (it returns
`False` for a stage with no required units at all), so the guidance panel could
never say "finished" again for any project. Contrast Task 6, where vertical joined
stages that sit below `rendered` and finished projects visibly, correctly, dropped.

**Why the frame comes from `build/video_wide.mp4` and not `output/final_wide.mp4`.**
The deliverable has the subtitles burned into it. A thumbnail cut from it would
carry a line of caption across the picture, under the headline this module draws —
two sets of words fighting at 210 px wide. The assembly is the same picture without
them. The finished render is kept as a fallback for a project whose `build/` has
been cleaned away, and the difference is invisible to the cache because the run-time
hash covers whichever file it actually read.

**Why the scrim is as heavy as it is.** White display text over an unknown
photograph is a coin toss; over a blown-out sky it is invisible. The ramp is sized
so that the *whole* text box clears WCAG AA against white — 4.5:1 — for a source
frame that is pure white, which is the worst case a photograph can hand us. That
makes the bottom of the picture dark, on purpose: a lower-third scrim is what turns
a frame with words on it into a thumbnail.

The face is **Space Grotesk Bold**, read from `videomaker.assets.DISPLAY_FONT` —
the copy Task 20 already bundled for the review UI. No second family, no second
licence entry in `NOTICE.md`; see `docs/ui-design.md` for the coordination.
"""

from math import ceil
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from videomaker.assets import DISPLAY_FONT
from videomaker.cache import hash_inputs, stage_key
from videomaker.media.ffmpeg import extract_frame
from videomaker.models import Aspect, Project
from videomaker.pipeline.assemble import scene_timeline, video_relpath
from videomaker.pipeline.base import (
    SCENE_GAP_S,
    StageDeps,
    StageResult,
    content_hash,
    project_root,
)
from videomaker.pipeline.render import OUTPUT_DIRNAME, output_relpath

STAGE = "thumbnail"

#: The aspect the thumbnail is cut from. The long cut, not the Short: a 9:16 frame
#: letterboxed into a 16:9 card is mostly bars, and every platform that wants a
#: custom thumbnail wants a wide one.
THUMBNAIL_ASPECT = Aspect.WIDE

#: YouTube's own recommendation, and the smallest size that is still sharp on a TV.
THUMB_WIDTH = 1280
THUMB_HEIGHT = 720

#: Where the picture is written, relative to the project folder.
THUMBNAIL_FILENAME = "thumbnail.jpg"

#: The frame is extracted here first, then deleted. Inside the project so the M0
#: relative-path rule holds for the ffmpeg call.
FRAME_RELPATH = "build/.thumbnail-frame.png"

#: High enough that the scrim shows no banding, low enough to stay well under the
#: 2 MB every platform caps a custom thumbnail at.
JPEG_QUALITY = 90

# ------------------------------------------------------------------------ layout

#: Space Grotesk Bold — the face `docs/ui-design.md` assigns to thumbnail text, and
#: the copy Task 20 already put in the wheel. Read, never bundled a second time.
FONT_PATH: Path = DISPLAY_FONT

MARGIN = 64
#: The brand mark's rail, at thumbnail scale: one accent bar beside the headline.
RULE_WIDTH = 10
RULE_GAP = 26
#: Tall enough for three lines at `MAX_FONT_PX`, and no taller.
TEXT_BLOCK_HEIGHT = 340

MAX_FONT_PX = 104
MIN_FONT_PX = 44
FONT_STEP_PX = 2
MAX_LINES = 3
LINE_HEIGHT = 1.08
ELLIPSIS = "…"

TEXT_COLOUR = (255, 255, 255)
#: `--accent` from the dark palette in `docs/ui-design.md`: the scrim is dark, so
#: the dark theme's accent is the one that reads against it.
ACCENT_COLOUR = (166, 174, 255)

#: How far above the headline the scrim starts to appear, as a fraction of the
#: height. It is measured from the text rather than from the top edge so a
#: one-line headline leaves nearly three quarters of the picture untouched and a
#: three-line one still gets the full ramp it needs.
SCRIM_RAMP_FRACTION = 0.34
#: Alpha the ramp has reached by the top of the text box. Sized from the contrast
#: requirement, not by eye: 170 over pure white leaves 85, which is 7.4:1 against
#: the white the headline is drawn in.
SCRIM_TEXT_ALPHA = 170
#: ...and it keeps deepening to here at the very bottom edge.
SCRIM_ALPHA = 228
#: Ease-in on the upper ramp, so the scrim arrives rather than starting as a band.
SCRIM_GAMMA = 1.7


def thumbnail_relpath() -> str:
    return f"{OUTPUT_DIRNAME}/{THUMBNAIL_FILENAME}"


# ---------------------------------------------------------------------- the words


def headline(project: Project) -> str:
    """The words on the picture: the author's override, else the topic."""
    return (project.thumbnail_text or project.topic).strip()


def display_text(text: str) -> str:
    """Caps for a machine-made headline; the author's own casing when they chose one.

    Topics arrive slug-shaped and lowercase (`how ssds work`), and a display face set
    in caps is what a thumbnail wants. But `thumbnail_text` is a lever the author
    pulled deliberately, so the moment a capital appears in the string we take that
    as an authored decision and stop shouting over it.
    """
    return text.upper() if text == text.lower() else text


# ----------------------------------------------------------------------- the type


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_PATH), size)


def _split_word(word: str, font: ImageFont.FreeTypeFont, max_width: float) -> list[str]:
    """Break one over-wide word into pieces that fit. A URL is still a headline."""
    pieces: list[str] = []
    current = ""
    for character in word:
        if current and font.getlength(current + character) > max_width:
            pieces.append(current)
            current = character
        else:
            current += character
    if current:
        pieces.append(current)
    return pieces or [word]


def wrap_lines(text: str, font: ImageFont.FreeTypeFont, max_width: float) -> list[str]:
    """Greedy word wrap, falling back to a character break for an unbreakable word."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}" if current else word
        if font.getlength(candidate) <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
            current = ""
        if font.getlength(word) <= max_width:
            current = word
            continue
        for piece in _split_word(word, font, max_width):
            if current:
                lines.append(current)
            current = piece
    if current:
        lines.append(current)
    return lines


def block_height(font: ImageFont.FreeTypeFont, line_count: int) -> int:
    """How tall `line_count` lines actually draw, from the face's own metrics.

    Measured rather than assumed, because the leading and the ascender are not the
    same number: fitting on `size * LINE_HEIGHT` alone lets a three-line block hang
    out of the box it was told it fitted.
    """
    if line_count <= 0:
        return 0
    ascent, descent = font.getmetrics()
    return round((line_count - 1) * font.size * LINE_HEIGHT) + ascent + descent


def _ellipsise(line: str, font: ImageFont.FreeTypeFont, max_width: float) -> str:
    """Trim `line` until it and an ellipsis fit."""
    trimmed = line
    while trimmed and font.getlength(trimmed + ELLIPSIS) > max_width:
        trimmed = trimmed[:-1]
    return trimmed.rstrip() + ELLIPSIS


def fit_headline(text: str, max_width: float, max_height: float) -> tuple[int, list[str]]:
    """The largest size at which `text` wraps into at most `MAX_LINES` inside the box.

    Steps down rather than solving, because the wrap is discrete: a two-point change
    in size can drop a line, and there is no closed form for where. Thirty-one
    `getlength` passes cost nothing next to the encode that preceded them.

    When even `MIN_FONT_PX` cannot hold it, the last line is ellipsised. Shrinking
    further would put six-point type on a card read at 210 px wide, which is not a
    smaller headline, it is no headline.
    """
    display = text.strip()
    if not display:
        return MAX_FONT_PX, []
    for size in range(MAX_FONT_PX, MIN_FONT_PX - 1, -FONT_STEP_PX):
        font = _font(size)
        lines = wrap_lines(display, font, max_width)
        if len(lines) <= MAX_LINES and block_height(font, len(lines)) <= max_height:
            return size, lines
    font = _font(MIN_FONT_PX)
    lines = wrap_lines(display, font, max_width)[:MAX_LINES]
    lines[-1] = _ellipsise(lines[-1], font, max_width)
    return MIN_FONT_PX, lines


# --------------------------------------------------------------------- the picture


def text_box(size: tuple[int, int]) -> tuple[int, int, int, int]:
    """`(left, top, right, bottom)` of the headline's box, in pixels.

    One definition, read by the composer (to place type), by the scrim (to know how
    far up it must be dark) and by the tests (to measure both). Two definitions is
    how a scrim ends up not quite covering the text it was drawn for.
    """
    width, height = size
    left = MARGIN + RULE_WIDTH + RULE_GAP
    bottom = height - MARGIN
    return left, bottom - TEXT_BLOCK_HEIGHT, width - MARGIN, bottom


def cover_fit(frame: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Scale `frame` to cover `size` and centre-crop the overflow. Never letterbox.

    Black bars on a thumbnail read as a broken upload, and every platform crops the
    card to 16:9 anyway — better to choose the crop here than to have it chosen.
    """
    width, height = size
    scale = max(width / frame.width, height / frame.height)
    # `ceil`, not `round`: rounding down by a pixel on either axis would leave the
    # crop reaching outside the scaled image, and Pillow pads that with black — one
    # hairline of letterbox, which is the exact thing this function exists to avoid.
    scaled = frame.resize(
        (ceil(frame.width * scale), ceil(frame.height * scale)),
        Image.LANCZOS,
    )
    left = (scaled.width - width) // 2
    top = (scaled.height - height) // 2
    return scaled.crop((left, top, left + width, top + height))


def scrim(size: tuple[int, int], *, text_top: int | None = None) -> Image.Image:
    """The black gradient overlay, as an RGBA image the caller composites.

    Three regions, in one monotonic ramp: nothing above the ramp start, an eased
    rise to `SCRIM_TEXT_ALPHA` by the time it reaches the type, then a gentler slide
    to `SCRIM_ALPHA` at the bottom edge. The middle number is the load-bearing one —
    it is what carries white text past 4.5:1 over a white frame — and the last one
    exists so the bottom edge reads as an edge rather than a flat panel.

    `text_top` is where the headline actually starts. The composer passes the block
    it just measured, so a short headline dims only the bottom quarter and the shot
    survives; the default is the top of the whole text box, which is the worst case
    and the one the contrast guarantee is stated against.
    """
    height = size[1]
    text_top = text_box(size)[1] if text_top is None else text_top
    ramp_top = max(0, round(text_top - height * SCRIM_RAMP_FRACTION))

    column = Image.new("L", (1, height), 0)
    for y in range(height):
        if y <= ramp_top:
            alpha = 0
        elif y < text_top:
            progress = (y - ramp_top) / max(text_top - ramp_top, 1)
            alpha = SCRIM_TEXT_ALPHA * (progress**SCRIM_GAMMA)
        else:
            progress = (y - text_top) / max(height - 1 - text_top, 1)
            alpha = SCRIM_TEXT_ALPHA + (SCRIM_ALPHA - SCRIM_TEXT_ALPHA) * progress
        column.putpixel((0, y), round(alpha))

    overlay = Image.new("RGBA", size, (0, 0, 0, 255))
    overlay.putalpha(column.resize(size, Image.NEAREST))
    return overlay


def compose_thumbnail(frame: Image.Image, text: str) -> Image.Image:
    """Cover-fit `frame`, lay the scrim over it, and set `text` in the box."""
    size = (THUMB_WIDTH, THUMB_HEIGHT)
    left, top, right, bottom = text_box(size)
    size_px, lines = fit_headline(display_text(text), right - left, bottom - top)
    font = _font(size_px)
    block_top = max(top, bottom - block_height(font, len(lines))) if lines else top

    picture = cover_fit(frame.convert("RGB"), size).convert("RGBA")
    picture = Image.alpha_composite(picture, scrim(size, text_top=block_top))
    if not lines:
        return picture.convert("RGB")

    ascent, _ = font.getmetrics()
    step = round(size_px * LINE_HEIGHT)

    draw = ImageDraw.Draw(picture)
    draw.rectangle(
        (MARGIN, block_top, MARGIN + RULE_WIDTH - 1, bottom - 1),
        fill=(*ACCENT_COLOUR, 255),
    )
    for index, line in enumerate(lines):
        draw.text(
            (left, block_top + index * step + ascent),
            line,
            font=font,
            fill=(*TEXT_COLOUR, 255),
            anchor="ls",
        )
    return picture.convert("RGB")


# ------------------------------------------------------------------------ the stage


def source_video(root: Path) -> Path | None:
    """The wide picture to cut a frame from, or `None` if nothing is assembled yet.

    The assembly first — it has no captions burned into it. The deliverable is the
    fallback, for a project whose `build/` has been cleaned away.
    """
    for relpath in (video_relpath(THUMBNAIL_ASPECT), output_relpath(THUMBNAIL_ASPECT)):
        candidate = root / relpath
        if candidate.is_file():
            return candidate
    return None


def frame_time_s(project: Project) -> float:
    """The middle of the opening shot, or zero if there is no timeline.

    The middle, so the grab can never land on a cut, where a frame is whatever the
    encoder was doing between two shots. The opening shot, because it is the one the
    author chose to lead with — and because "the longest" or "the best" are editorial
    judgements this tool does not make on its own (spec 4.5).
    """
    segments = scene_timeline(project, gap_s=SCENE_GAP_S, aspect=THUMBNAIL_ASPECT)
    if not segments:
        return 0.0
    return segments[0].start_s + segments[0].duration_s / 2


def thumbnail_hash(root: Path, *, source: Path, text: str, at_s: float) -> str:
    """The source frame's bytes, the words, the face, and every layout knob.

    The font file is hashed rather than named: updating Space Grotesk changes the
    picture, and a thumbnail that silently keeps the old face after a font bump is a
    stale artefact the cache claims is fresh.
    """
    return hash_inputs(
        source=content_hash(source),
        at_s=at_s,
        text=display_text(text),
        font=content_hash(FONT_PATH),
        size=[THUMB_WIDTH, THUMB_HEIGHT],
        layout=[MARGIN, RULE_WIDTH, RULE_GAP, TEXT_BLOCK_HEIGHT, MAX_LINES, LINE_HEIGHT],
        type=[MAX_FONT_PX, MIN_FONT_PX, FONT_STEP_PX],
        colour=[list(TEXT_COLOUR), list(ACCENT_COLOUR)],
        scrim=[SCRIM_RAMP_FRACTION, SCRIM_TEXT_ALPHA, SCRIM_ALPHA, SCRIM_GAMMA],
        quality=JPEG_QUALITY,
    )


def grab_frame(root: Path, source: Path, at_s: float) -> Image.Image:
    """One decoded frame, as a PIL image. Falls back to the first frame.

    The seek can land past the end of a source that is shorter than the timeline
    says — a truncated encode, a hand-edited `project.json` — and ffmpeg then exits
    zero having written nothing. Retrying at zero is better than failing the stage
    over which frame we got.
    """
    frame_path = root / FRAME_RELPATH
    frame_path.parent.mkdir(parents=True, exist_ok=True)
    frame_path.unlink(missing_ok=True)
    relative = source.relative_to(root).as_posix()
    extract_frame(relative, at_s=at_s, dest=FRAME_RELPATH, cwd=root)
    if not frame_path.is_file():
        extract_frame(relative, at_s=0.0, dest=FRAME_RELPATH, cwd=root)
    try:
        with Image.open(frame_path) as image:
            return image.convert("RGB")
    finally:
        frame_path.unlink(missing_ok=True)


def run_thumbnail(project: Project, deps: StageDeps) -> StageResult:
    """Draw `output/thumbnail.jpg`, once, cached on `thumbnail:all`.

    A no-op for a project with nothing assembled: the runner walks every stage on
    every run, so this is reached long before there is a picture to cut, and a stage
    that raised there would fail runs that are going exactly as designed.
    """
    root = project_root(deps, project)
    source = source_video(root)
    if source is None:
        return StageResult(changed=False, skipped_units=1)

    text = headline(project)
    at_s = frame_time_s(project)
    key = stage_key(STAGE)
    current = thumbnail_hash(root, source=source, text=text, at_s=at_s)
    out_path = root / thumbnail_relpath()
    if not deps.stage_cache.is_stale(key, current) and out_path.is_file():
        return StageResult(changed=False, skipped_units=1)

    picture = compose_thumbnail(grab_frame(root, source, at_s), text)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    picture.save(out_path, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)

    project.thumbnail_path = thumbnail_relpath()
    deps.stage_cache.mark(key, current)
    deps.stage_cache.save()
    deps.store.save(project)
    return StageResult(changed=True)
