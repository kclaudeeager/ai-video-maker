"""Write burned-in captions as an Advanced SubStation Alpha (.ass) file.

Two things bite here and both are covered by unit tests:

* ASS colours are ``&HAABBGGRR`` — **BGR**, not RGB.
* Whisper word timestamps are zero-gap (M0 finding 3): each word starts exactly
  where the previous one ended. Chunk boundaries must be inset or caption blocks
  touch edge-to-edge on screen.
* Whisper sometimes returns ``start == end == 0.0`` for a run of words. Insetting
  such a chunk drives its end back onto its start, and a zero-duration
  ``Dialogue`` line never displays at all — so chunks below
  :data:`MIN_DISPLAY_DURATION_S` are merged away rather than written. Karaoke
  multiplies the number of ``Dialogue`` events by the words in a chunk, so
  :func:`karaoke_spans` re-establishes that guarantee per word.
"""

from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

from videomaker.models import Aspect, WordTiming

#: Shrink each chunk's end by this much so consecutive captions never touch.
CHUNK_INSET_S = 0.040

#: A chunk is never shortened below this, however tight the timings are.
MIN_CHUNK_DURATION_S = 0.200

#: A chunk shorter than this never displays long enough to be read, so it is
#: never written as a ``Dialogue`` line. Degenerate word timings can still slip
#: past :data:`MIN_CHUNK_DURATION_S`, because the clamp above is followed by a
#: ``min(end, next_start)`` that pulls the end back to the start when the next
#: chunk begins at the same instant — which is exactly how M1 shipped a
#: ``0:00:22.50,0:00:22.50`` line (spike follow-up 1).
MIN_DISPLAY_DURATION_S = 0.150

#: A karaoke event shorter than this is a flicker rather than a highlight — it can
#: land between two rendered frames and never be seen. A chunk whose window cannot
#: give every word this much is written as one plain line instead (see
#: :func:`karaoke_spans`), which is why karaoke can never reintroduce the M1 defect.
MIN_KARAOKE_SPAN_S = 0.060

#: Widely available on Linux; libass falls back gracefully if it is missing.
DEFAULT_FONT = "DejaVu Sans"


def ass_colour(r: int, g: int, b: int) -> str:
    """Format an RGB triple as an ASS ``&HAABBGGRR`` colour (opaque, BGR order)."""
    return f"&H00{b:02X}{g:02X}{r:02X}"


def override_colour(colour: str) -> str:
    """Turn a style colour (``&HAABBGGRR``) into an override tag's ``&HBBGGRR&``.

    Inline ``\\1c`` tags take a colour with no alpha byte and a trailing ``&``. This
    slices the alpha off an existing :func:`ass_colour` result rather than
    re-formatting the components, so there is exactly one place in this module that
    knows the byte order is BGR.
    """
    return f"&H{colour[4:]}&"


#: Amber ``#FFD60A`` — the word currently being spoken. Built through
#: :func:`ass_colour` because writing the literal by hand in RGB order renders a
#: completely different colour and nothing anywhere reports an error.
KARAOKE_HIGHLIGHT_COLOUR = ass_colour(255, 214, 10)


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
    # Authored for 1080x1920 from scratch. Every number below is a literal on
    # purpose: deriving them from the wide entry (`font_size * 1.5`, and so on)
    # would silently re-lay-out the Short the next time the wide style is tuned,
    # and spec 4.5 is explicit that neither aspect is scaled from the other.
    #
    # `margin_v` is measured up from the bottom of the frame (alignment 2), and is
    # picked so a single 96px line's centre lands at 62% of a 1920-high frame:
    # 1920 - 672 - (96 * 1.2 / 2) == 1190.4 == 0.620 * 1920. Sitting the text above
    # the middle keeps it clear of a phone's bottom UI chrome without pushing it
    # into the subject's face.
    Aspect.VERTICAL: CaptionStyle(
        font_size=96,
        words_per_chunk=3,
        margin_v=672,
        alignment=2,
        primary_colour=ass_colour(255, 255, 255),
        outline_colour=ass_colour(0, 0, 0),
        outline=6.0,  # a 96px face needs a heavier rim to survive busy stock footage
        shadow=2.0,
    ),
}


def chunk_words(words: list[WordTiming], per_chunk: int) -> list[list[WordTiming]]:
    """Group ``words`` into caption chunks, insetting each chunk's end.

    The input is assumed to be zero-gap, so the last word of every chunk is
    copied with an earlier ``end_s``: back off by :data:`CHUNK_INSET_S`, keep at
    least :data:`MIN_CHUNK_DURATION_S` on screen, and never run into the next
    chunk's start.

    Degenerate word timings survive all three of those rules, so the result is
    finally passed through :func:`merge_degenerate_chunks`: no chunk this function
    returns is shorter than :data:`MIN_DISPLAY_DURATION_S`.
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
    return merge_degenerate_chunks(chunks)


def chunk_duration(chunk: list[WordTiming]) -> float:
    """How long ``chunk`` is on screen: exactly what :func:`write_ass` timestamps."""
    return chunk[-1].end_s - chunk[0].start_s


def merge_degenerate_chunks(chunks: list[list[WordTiming]]) -> list[list[WordTiming]]:
    """Fold any chunk below :data:`MIN_DISPLAY_DURATION_S` into the one after it.

    Merging rather than dropping, because dropping silently captions those words
    nowhere — the visible half of the M1 defect. The absorbed words keep their own
    start (the earliest honest moment for them) and stay on screen for the whole of
    the following chunk's window, so nothing is lost and nothing runs ahead of the
    narration by more than it already did.

    Merging only ever happens *within one call*, and :func:`chunk_grouped` calls this
    function once per scene, so a merge can never pull words across a scene cut.
    The last chunk of a run is never degenerate — it has no ``next_start`` to be
    clamped against, so :data:`MIN_CHUNK_DURATION_S` stands — which is what
    guarantees this pass always has somewhere to fold into.
    """
    merged: list[list[WordTiming]] = []
    for chunk in reversed(chunks):
        if merged and chunk_duration(chunk) < MIN_DISPLAY_DURATION_S:
            merged[-1] = [*chunk, *merged[-1]]  # merged[-1] is the chunk that follows
        else:
            merged.append(chunk)
    merged.reverse()
    return merged


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


def karaoke_spans(chunk: list[WordTiming]) -> list[tuple[float, float]]:
    """One (start, end) window per word, tiling the chunk's own display window.

    Karaoke multiplies the number of ``Dialogue`` events by the number of words in a
    chunk, so every guard the chunker earned has to be re-earned per word:

    * The windows are clipped into ``[chunk start, chunk end]`` and forced monotonic,
      so a highlight never runs before its caption appears or past where it leaves.
    * Whisper's degenerate runs (``start == end == 0.0``) collapse several words onto
      the same instant. Rather than emit those as zero-duration events — the exact M1
      defect — the whole chunk falls back to an even division of its window.
    * If even *that* cannot give each word :data:`MIN_KARAOKE_SPAN_S`, the result is
      empty and the caller writes one plain line: a caption nobody can read the
      highlight on is still a caption, whereas a flickering one is a regression.
    """
    start, end = chunk[0].start_s, chunk[-1].end_s
    bounds = [start]
    for word in chunk[1:]:
        bounds.append(min(max(word.start_s, bounds[-1]), end))
    bounds.append(end)

    spans = list(pairwise(bounds))
    if all(span_end - span_start >= MIN_KARAOKE_SPAN_S for span_start, span_end in spans):
        return spans

    step = (end - start) / len(chunk)
    if step < MIN_KARAOKE_SPAN_S:
        return []
    return [(start + index * step, start + (index + 1) * step) for index in range(len(chunk))]


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


def _dialogue(start: float, end: float, text: str) -> str:
    return f"Dialogue: 0,{format_timestamp(start)},{format_timestamp(end)},Default,,0,0,0,,{text}\n"


def _karaoke_lines(chunk: list[WordTiming], style: CaptionStyle) -> list[str]:
    """One ``Dialogue`` line per word, the spoken word recoloured, or ``[]``.

    The override blocks are appended *after* :func:`escape_text` has run over the
    narration, so the tags this function emits stay live while a word that itself
    contains braces stays escaped — the two must not be done in one pass.
    """
    tokens = [escape_text(word.word.strip()) for word in chunk]
    spans = karaoke_spans(chunk) if all(tokens) else []
    if not spans:
        return []

    highlight = f"{{\\1c{override_colour(KARAOKE_HIGHLIGHT_COLOUR)}}}"
    reset = f"{{\\1c{override_colour(style.primary_colour)}}}"
    lines = []
    for index, (start, end) in enumerate(spans):
        painted = [*tokens]
        painted[index] = f"{highlight}{painted[index]}{reset}"
        lines.append(_dialogue(start, end, " ".join(painted)))
    return lines


def write_ass(
    words: list[WordTiming],
    style: CaptionStyle,
    out_path: Path,
    *,
    play_res: tuple[int, int],
    groups: list[list[WordTiming]] | None = None,
    karaoke: bool = False,
) -> Path:
    """Write ``words`` as caption chunks to ``out_path`` and return that path.

    Pass ``groups`` (one list per scene) to keep chunks inside scene boundaries;
    ``words`` is then only the flat fallback. See :func:`chunk_grouped`.

    ``karaoke`` writes each chunk as one ``Dialogue`` event per word with the word
    being spoken recoloured to :data:`KARAOKE_HIGHLIGHT_COLOUR` — the vertical
    word-pop look. It defaults off, and off is byte-for-byte M1's output: the wide
    ``.ass`` is fingerprinted in every rendered project's stage cache.
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
        # Last line of defence: a line that never displays is worse than no line,
        # because it hides its words *and* leaves the next caption looking early.
        # `chunk_words` should already have merged these away.
        if chunk_duration(chunk) < MIN_DISPLAY_DURATION_S:
            continue
        # A chunk too tight to split falls through to the plain line below, so
        # karaoke can never lose words that the non-karaoke path would have kept.
        if karaoke and (karaoke_lines := _karaoke_lines(chunk, style)):
            lines.extend(karaoke_lines)
            continue
        lines.append(_dialogue(chunk[0].start_s, chunk[-1].end_s, text))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("".join(lines), encoding="utf-8")
    return out_path
