"""Write burned-in captions as an Advanced SubStation Alpha (.ass) file.

Two things bite here and both are covered by unit tests:

* ASS colours are ``&HAABBGGRR`` — **BGR**, not RGB.
* Whisper word timestamps are zero-gap (M0 finding 3): each word starts exactly
  where the previous one ended. Chunk boundaries must be inset or caption blocks
  touch edge-to-edge on screen.
"""

from dataclasses import dataclass
from pathlib import Path

from videomaker.models import Aspect, WordTiming

#: Shrink each chunk's end by this much so consecutive captions never touch.
CHUNK_INSET_S = 0.040

#: A chunk is never shortened below this, however tight the timings are.
MIN_CHUNK_DURATION_S = 0.200

#: Widely available on Linux; libass falls back gracefully if it is missing.
DEFAULT_FONT = "DejaVu Sans"


def ass_colour(r: int, g: int, b: int) -> str:
    """Format an RGB triple as an ASS ``&HAABBGGRR`` colour (opaque, BGR order)."""
    return f"&H00{b:02X}{g:02X}{r:02X}"


@dataclass(frozen=True)
class CaptionStyle:
    font_size: int
    words_per_chunk: int
    margin_v: int
    alignment: int  # ASS numpad alignment: 2 == bottom centre
    primary_colour: str
    outline_colour: str
    outline: float
    shadow: float
    font_name: str = DEFAULT_FONT


#: Per-aspect caption layout. M1 is wide only; M3 adds a vertical entry that is
#: authored from scratch — never derived by scaling these numbers.
STYLES: dict[Aspect, CaptionStyle] = {
    Aspect.WIDE: CaptionStyle(
        font_size=64,
        words_per_chunk=5,
        margin_v=160,  # lower third of a 1080-high frame
        alignment=2,
        primary_colour=ass_colour(255, 255, 255),
        outline_colour=ass_colour(0, 0, 0),
        outline=3.0,
        shadow=1.0,
    ),
}


def chunk_words(words: list[WordTiming], per_chunk: int) -> list[list[WordTiming]]:
    """Group ``words`` into caption chunks, insetting each chunk's end.

    The input is assumed to be zero-gap, so the last word of every chunk is
    copied with an earlier ``end_s``: back off by :data:`CHUNK_INSET_S`, keep at
    least :data:`MIN_CHUNK_DURATION_S` on screen, and never run into the next
    chunk's start.
    """
    if per_chunk < 1:
        raise ValueError("per_chunk must be at least 1")
    groups = [words[i : i + per_chunk] for i in range(0, len(words), per_chunk)]

    chunks: list[list[WordTiming]] = []
    for index, group in enumerate(groups):
        start = group[0].start_s
        end = group[-1].end_s
        next_start = groups[index + 1][0].start_s if index + 1 < len(groups) else None

        if next_start is not None and next_start - end < CHUNK_INSET_S:
            end = next_start - CHUNK_INSET_S
        end = max(end, start + MIN_CHUNK_DURATION_S)
        if next_start is not None:
            end = min(end, next_start)

        chunks.append([*group[:-1], group[-1].model_copy(update={"end_s": end})])
    return chunks


def chunk_grouped(groups: list[list[WordTiming]], per_chunk: int) -> list[list[WordTiming]]:
    """Chunk each group independently so a caption never mixes two groups' words.

    Callers pass one group per scene. Chunking the whole timeline as one run lets a
    chunk straddle a scene cut — the words are correctly timed but read as a mixture
    of two narrations, and the caption visibly spans the inter-scene gap.
    """
    chunks: list[list[WordTiming]] = []
    for group in groups:
        if group:
            chunks.extend(chunk_words(group, per_chunk))
    return chunks


def format_timestamp(seconds: float) -> str:
    """Format seconds as ASS ``H:MM:SS.cc`` (centiseconds)."""
    centiseconds = max(0, round(seconds * 100))
    hours, centiseconds = divmod(centiseconds, 360_000)
    minutes, centiseconds = divmod(centiseconds, 6_000)
    whole_seconds, centiseconds = divmod(centiseconds, 100)
    return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{centiseconds:02d}"


def escape_text(text: str) -> str:
    """Escape narration for an ASS ``Dialogue`` line.

    Braces open and close override blocks, so unescaped ``{drop}`` would be
    parsed as a (bogus) tag and vanish silently.
    """
    return (
        text.replace("\\", "\\\\")
        .replace("{", "\\{")
        .replace("}", "\\}")
        .replace("\r\n", " ")
        .replace("\n", " ")
        .replace("\r", " ")
    )


STYLE_FORMAT = (
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour,"
    " BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle,"
    " BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"
)
EVENT_FORMAT = "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"


def _style_line(style: CaptionStyle) -> str:
    return (
        "Style: Default,"
        f"{style.font_name},{style.font_size},"
        f"{style.primary_colour},{style.primary_colour},"
        f"{style.outline_colour},{ass_colour(0, 0, 0)},"
        f"-1,0,0,0,100,100,0,0,1,{style.outline:g},{style.shadow:g},"
        f"{style.alignment},60,60,{style.margin_v},1"
    )


def _header(style: CaptionStyle, play_res: tuple[int, int]) -> str:
    width, height = play_res
    return "\n".join(
        [
            "[Script Info]",
            "ScriptType: v4.00+",
            f"PlayResX: {width}",
            f"PlayResY: {height}",
            "WrapStyle: 2",
            "ScaledBorderAndShadow: yes",
            "",
            "[V4+ Styles]",
            STYLE_FORMAT,
            _style_line(style),
            "",
            "[Events]",
            EVENT_FORMAT,
            "",
        ]
    )


def write_ass(
    words: list[WordTiming],
    style: CaptionStyle,
    out_path: Path,
    *,
    play_res: tuple[int, int],
    groups: list[list[WordTiming]] | None = None,
) -> Path:
    """Write ``words`` as caption chunks to ``out_path`` and return that path.

    Pass ``groups`` (one list per scene) to keep chunks inside scene boundaries;
    ``words`` is then only the flat fallback. See :func:`chunk_grouped`.
    """
    chunks = (
        chunk_grouped(groups, style.words_per_chunk)
        if groups is not None
        else chunk_words(words, style.words_per_chunk)
    )
    lines = [_header(style, play_res)]
    for chunk in chunks:
        text = escape_text(" ".join(word.word for word in chunk).strip())
        if not text:
            continue
        start = format_timestamp(chunk[0].start_s)
        end = format_timestamp(chunk[-1].end_s)
        lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}\n")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("".join(lines), encoding="utf-8")
    return out_path
