"""Do the captions actually stay inside the frame? Asked of **real libass**.

M3 spike defect 2: `version number, allowing` came off `final_vertical.mp4` as
`ersion number, allowin`, with glyphs at x = 5 and x = 1079 in a 1080-wide frame
against a declared 60 px margin. Re-measured over the whole workspace in the units
libass actually draws in, 27 of 185 vertical caption lines ran off the frame and
50 ran past their text box, the widest at 1670 px.

`tests/unit/test_ass_writer.py` measures the same thing with the font's own
metrics, which is fast and runs everywhere. This module is the one that cannot be
fooled: it hands the written `.ass` to the same `subtitles=` filter `render` uses,
over a flat black frame, and reads the burned-in **pixels** back. A wrap mode the
writer declares but libass ignores, a margin libass measures from somewhere else,
an outline that draws outside the box it was given — none of those can hide here.

Flat black and a hard threshold, because every lit pixel in the result is then
caption: there is nothing else in the picture. The allowance below the declared
margin is the rim: libass draws `Outline` and `Shadow` *outside* the text box, so
ink is expected that far past it and no further.
"""

import subprocess
from math import ceil

import pytest
from PIL import Image

from videomaker.media.ass import STYLES, write_ass
from videomaker.models import Aspect, WordTiming
from videomaker.pipeline.captions import PLAY_RES

#: A pixel this bright over a black frame is caption ink, rim and shadow included.
INK = 40

#: Narration taken from the owner's own workspace. The vertical chunks all lost
#: letters off the frame; the wide ones are here because the same guarantee has to
#: hold for the long cut, which currently survives on arithmetic rather than design.
#: `representations—plain` is the hard one: one unbroken token 1055 px wide in a
#: 936 px box, which no wrap mode can rescue — libass breaks on spaces and on nothing
#: else, not on a dash, not on U+200B, not on a soft hyphen (all three measured).
PHRASES: dict[Aspect, tuple[str, ...]] = {
    Aspect.VERTICAL: (
        "version number, allowing",
        "modern computing hardware.",
        "several representations—plain",
        "instantly reachable but",
    ),
    Aspect.WIDE: (
        "cloud counterparts automatically, continuously throughout",
        "stores several representations—plain text, HTML,",
    ),
}

#: One chunk per phrase, two seconds each, so a frame grab at `index * 2 + 1`
#: lands squarely inside chunk `index`.
CHUNK_S = 2.0


def _groups(phrases: tuple[str, ...]) -> list[list[WordTiming]]:
    groups = []
    for index, phrase in enumerate(phrases):
        words = phrase.split()
        step = CHUNK_S / len(words)
        start = index * CHUNK_S
        groups.append(
            [
                WordTiming(word=word, start_s=start + n * step, end_s=start + (n + 1) * step)
                for n, word in enumerate(words)
            ]
        )
    return groups


def _burn(ass_path, size, at_s, out_path):
    """One frame of black with `ass_path` burned over it, through the real filter.

    Run from the `.ass` file's own directory and referred to by bare filename, for
    the same reason `render.subtitles_filter` uses a project-relative path: the
    filter argument is parsed as a filter-graph token and an absolute path's colons
    would need escaping.
    """
    width, height = size
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y", "-nostdin",
            "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:r=5:d=60",
            "-vf", f"subtitles={ass_path.name}",
            "-ss", str(at_s), "-frames:v", "1",
            out_path.name,
        ],
        cwd=ass_path.parent,
        check=True,
        capture_output=True,
    )
    return out_path


def _ink_columns(png):
    """`(leftmost, rightmost)` column carrying ink, or `None` for an empty frame."""
    with Image.open(png) as image:
        grey = image.convert("L")
    width, height = grey.size
    pixels = grey.load()
    left, right = width, -1
    for y in range(height):
        for x in range(left):
            if pixels[x, y] >= INK:
                left = x
                break
        for x in range(width - 1, right, -1):
            if pixels[x, y] >= INK:
                right = x
                break
    return None if right < 0 else (left, right)


@pytest.fixture(scope="module")
def burned(tmp_path_factory):
    """Every phrase of both aspects, written and burned once."""
    root = tmp_path_factory.mktemp("caption_frame_fit")
    frames = {}
    for aspect, phrases in PHRASES.items():
        style = STYLES[aspect]
        size = PLAY_RES[aspect]
        ass_path = write_ass([], style, root / f"{aspect.value}.ass", play_res=size,
                             groups=_groups(phrases))
        for index, phrase in enumerate(phrases):
            png = _burn(ass_path, size, index * CHUNK_S + CHUNK_S / 2,
                        root / f"{aspect.value}-{index}.png")
            frames[aspect, phrase] = _ink_columns(png)
    return frames


@pytest.mark.parametrize(
    ("aspect", "phrase"),
    [(aspect, phrase) for aspect, phrases in PHRASES.items() for phrase in phrases],
)
def test_no_caption_glyph_reaches_the_edge_of_the_frame(burned, aspect, phrase):
    """The defect in its own units: glyphs at x = 5 and x = 1079 of 1080."""
    style = STYLES[aspect]
    width, _ = PLAY_RES[aspect]
    # libass draws the rim outside the text box, so ink is expected this far past
    # the declared margin — and not one pixel further.
    rim = ceil(style.outline + style.shadow)
    columns = burned[aspect, phrase]

    assert columns is not None, f"{aspect.value}: {phrase!r} burned in nowhere"
    left, right = columns
    assert left >= style.margin_l - rim, (
        f"{aspect.value}: {phrase!r} draws ink at x={left}, inside a declared"
        f" {style.margin_l}px margin (rim {rim}px)"
    )
    assert right <= width - style.margin_r + rim, (
        f"{aspect.value}: {phrase!r} draws ink at x={right} of {width}, past a declared"
        f" {style.margin_r}px margin (rim {rim}px)"
    )


def test_the_vertical_caption_that_lost_letters_now_wraps_and_fits(burned):
    """`version number, allowing` rendered as `ersion number, allowin`."""
    left, right = burned[Aspect.VERTICAL, "version number, allowing"]
    assert (left, right) != (5, 1079)
    assert 0 < left < right < PLAY_RES[Aspect.VERTICAL][0] - 1


# ------------------------------------------------------------------- the metric

#: Two runs of the same glyph, differing by five. Rendered through the same style
#: they carry the same outline and the same shadow, so subtracting one ink width
#: from the other cancels the rim exactly and leaves five advances of pure type.
CALIBRATION = ("M" * 5, "M" * 10)


@pytest.mark.parametrize("aspect", list(PHRASES))
def test_the_type_metric_matches_what_libass_actually_draws(tmp_path, caption_face, aspect):
    """Pins the `caption_face` metric, which every fast caption measurement uses.

    ASS `Fontsize` is a line height rather than an em size: libass scales the face so
    that ascender plus descender comes to `Fontsize`, which for DejaVu Sans is 0.859
    em. Measuring at face value over-states a caption by a sixth — enough to call a
    line that fits an overflow, and enough to over-count a real one.

    The unit tests cannot see libass, so they take that scaling on trust. This is
    where it is checked against the renderer itself, in both aspects.
    """
    style = STYLES[aspect]
    size = PLAY_RES[aspect]
    ass_path = write_ass([], style, tmp_path / f"{aspect.value}-cal.ass", play_res=size,
                         groups=_groups(CALIBRATION))

    widths = []
    for index in range(len(CALIBRATION)):
        png = _burn(ass_path, size, index * CHUNK_S + CHUNK_S / 2,
                    tmp_path / f"cal-{index}.png")
        left, right = _ink_columns(png)
        widths.append(right - left)

    drawn = widths[1] - widths[0]
    expected = caption_face(style).getlength(CALIBRATION[0])
    assert drawn == pytest.approx(expected, rel=0.02), (
        f"{aspect.value}: libass advances {drawn}px for {CALIBRATION[0]!r} where the"
        f" test metric says {expected:.0f}px — LIBASS_PPEM_RATIO is wrong"
    )
