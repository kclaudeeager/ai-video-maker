import pytest

from videomaker.media.ass import (
    MIN_DISPLAY_DURATION_S,
    STYLES,
    ass_colour,
    chunk_words,
    write_ass,
)
from videomaker.models import Aspect, WordTiming
from videomaker.pipeline.captions import PLAY_RES

#: Index of the free-text field of a ``Dialogue:`` line: Layer, Start, End, Style,
#: Name, MarginL, MarginR, MarginV and Effect come first.
DIALOGUE_TEXT_FIELD = 9


def w(word, s, e): return WordTiming(word=word, start_s=s, end_s=e)


def _body(line):
    """The Text field of a ``Dialogue:`` line — everything after the 9th comma."""
    return line[len("Dialogue:"):].split(",", DIALOGUE_TEXT_FIELD)[DIALOGUE_TEXT_FIELD]


def test_ass_colour_is_bgr_not_rgb():
    assert ass_colour(255, 0, 0) == "&H000000FF"   # red
    assert ass_colour(0, 0, 255) == "&H00FF0000"   # blue
    assert ass_colour(255, 255, 255) == "&H00FFFFFF"


def test_chunks_respect_size():
    words = [w(str(i), i * 0.3, (i + 1) * 0.3) for i in range(11)]
    chunks = chunk_words(words, 5)
    assert [len(c) for c in chunks] == [5, 5, 1]


def test_chunks_are_inset_so_they_do_not_touch():
    words = [w(str(i), i * 0.5, (i + 1) * 0.5) for i in range(4)]  # zero-gap, as whisper emits
    chunks = chunk_words(words, 2)
    first_end = chunks[0][-1].end_s
    second_start = chunks[1][0].start_s
    assert first_end < second_start


def test_write_ass_emits_valid_header_and_events(tmp_path):
    words = [w("hello", 0.0, 0.5), w("world", 0.5, 1.0)]
    out = write_ass(words, STYLES[Aspect.WIDE], tmp_path / "c.ass", play_res=(1920, 1080))
    text = out.read_text()
    assert "[Script Info]" in text and "PlayResX: 1920" in text
    assert "[V4+ Styles]" in text and "[Events]" in text
    assert text.count("Dialogue:") == 1          # both words fit one chunk
    assert "hello world" in text


def test_timestamps_are_ass_formatted(tmp_path):
    words = [w("x", 3661.5, 3662.0)]  # 1:01:01.50
    out = write_ass(words, STYLES[Aspect.WIDE], tmp_path / "c.ass", play_res=(1920, 1080))
    assert "1:01:01.50" in out.read_text()


def test_braces_in_narration_are_escaped(tmp_path):
    # Unescaped { } would be parsed as ASS override tags and silently vanish.
    out = write_ass([w("{drop}", 0.0, 1.0)], STYLES[Aspect.WIDE], tmp_path / "c.ass",
                    play_res=(1920, 1080))
    assert "\\{drop\\}" in out.read_text() or "(drop)" in out.read_text()


def test_empty_word_list_writes_header_only(tmp_path):
    out = write_ass([], STYLES[Aspect.WIDE], tmp_path / "c.ass", play_res=(1920, 1080))
    assert "Dialogue:" not in out.read_text()


def _dialogue_spans(text):
    """Every ``Dialogue:`` line's (start, end) in seconds, parsed back from the file."""
    spans = []
    for line in text.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        _, start, end, *_ = line[len("Dialogue:"):].split(",")
        spans.append((_seconds(start), _seconds(end)))
    return spans


def _seconds(stamp):
    hours, minutes, rest = stamp.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(rest)


# M1 defect: whisper returned start == end == 0.0 for scene 3's first seven words,
# and the writer emitted `Dialogue: 0,0:00:22.50,0:00:22.50,...` — a line that never
# displays, so five words were captioned nowhere.
DEGENERATE = [w(f"word{i}", 0.0, 0.0) for i in range(7)] + [
    w("so", 2.08, 2.70), w("the", 2.70, 2.94), w("read", 2.94, 3.40),
]


def test_no_dialogue_line_has_end_at_or_before_start(tmp_path):
    out = write_ass(DEGENERATE, STYLES[Aspect.WIDE], tmp_path / "c.ass", play_res=(1920, 1080))
    spans = _dialogue_spans(out.read_text())
    assert spans, "degenerate timings must not silence the whole file"
    for start, end in spans:
        assert end > start, f"non-displaying Dialogue line {start} -> {end}"


def test_no_dialogue_line_has_end_at_or_before_start_when_grouped(tmp_path):
    # The production path: one group per scene (see `chunk_grouped`).
    groups = [DEGENERATE, [w("next", 5.0, 5.4), w("scene", 5.4, 5.9)]]
    out = write_ass([], STYLES[Aspect.WIDE], tmp_path / "c.ass",
                    play_res=(1920, 1080), groups=groups)
    spans = _dialogue_spans(out.read_text())
    assert spans
    for start, end in spans:
        assert end > start, f"non-displaying Dialogue line {start} -> {end}"


def test_degenerate_chunk_keeps_its_words_by_merging_forward(tmp_path):
    out = write_ass(DEGENERATE, STYLES[Aspect.WIDE], tmp_path / "c.ass", play_res=(1920, 1080))
    text = out.read_text()
    # Nothing is dropped silently: every word is still somewhere in the file.
    for word in DEGENERATE:
        assert word.word in text


def test_all_words_degenerate_still_emits_one_displayable_line(tmp_path):
    words = [w(f"w{i}", 0.0, 0.0) for i in range(10)]
    out = write_ass(words, STYLES[Aspect.WIDE], tmp_path / "c.ass", play_res=(1920, 1080))
    spans = _dialogue_spans(out.read_text())
    assert len(spans) == 1
    assert spans[0][1] - spans[0][0] >= MIN_DISPLAY_DURATION_S


def test_chunk_words_never_returns_a_chunk_below_the_display_floor():
    words = [w(f"w{i}", 0.0, 0.0) for i in range(7)] + [w("so", 2.08, 2.7)]
    for chunk in chunk_words(words, 5):
        assert chunk[-1].end_s - chunk[0].start_s >= MIN_DISPLAY_DURATION_S


def test_karaoke_defaults_off_so_the_wide_output_is_byte_identical(tmp_path):
    """The wide `.ass` is fingerprinted in every rendered project's stage cache."""
    words = [w("hello", 0.0, 0.5), w("world", 0.5, 1.0), w("again", 1.0, 1.6)]
    plain = write_ass(words, STYLES[Aspect.WIDE], tmp_path / "a.ass", play_res=(1920, 1080))
    explicit = write_ass(words, STYLES[Aspect.WIDE], tmp_path / "b.ass",
                         play_res=(1920, 1080), karaoke=False)
    assert plain.read_text() == explicit.read_text()


def test_karaoke_on_wide_still_never_emits_a_zero_duration_event(tmp_path):
    out = write_ass(DEGENERATE, STYLES[Aspect.WIDE], tmp_path / "k.ass",
                    play_res=(1920, 1080), karaoke=True)
    spans = _dialogue_spans(out.read_text())
    assert spans
    for start, end in spans:
        assert end > start, f"non-displaying Dialogue line {start} -> {end}"


# ------------------------------------------------- the frame: M3 spike defect 2

# Captions that run off the frame and lose letters.
#
# The M3 verification looked at `final_vertical.mp4` at full resolution and read
# `version number, allowing` as `ersion number, allowin`: the leading *v* and the
# trailing *g* were outside the 1080 px frame, with glyphs landing at x = 5 and
# x = 1079 against a declared 60 px margin. Measured over the owner's whole
# workspace, 50 of 185 vertical caption lines were wider than their text box and
# 27 of those were wider than the frame itself; the widest was 1670 px in a 1080 px
# frame. Wide lost no letters — 0 of 221 off the frame — but only by arithmetic, and
# one line was already past its box at 1832 px.
#
# The cause was `WrapStyle: 2` — *no wrapping at all*, so libass ran a long chunk
# off the screen rather than breaking it — and side margins hardcoded into the
# style line rather than authored per aspect.
#
# These tests measure the same thing the spike did, in the units libass draws in
# (see `LIBASS_PPEM_RATIO` in `tests/conftest.py`), laid out under the wrap mode
# the file itself declares. They are the equivalent of
# `test_the_headline_wraps_on_words_rather_than_overflowing`, which the thumbnail
# stage has had since M3 Task 20 and the captions never got.

#: WrapStyles that break a long line. 2 is "no word wrapping", which is the defect.
WRAPPING_MODES = frozenset({0, 1, 3})

#: The vertical chunks are lifted from the owner's own workspace, and every one of
#: them lost letters off the 1080 px frame.
#:
#: The wide chunk is **synthetic**, and that is the honest thing to say about it: no
#: wide line in the workspace overflows today, because five words at 64 px happen to
#: fit 1800 px. That is arithmetic, not a guarantee — a wordier script would put the
#: long cut in exactly the same place — so the guard is asserted against a chunk that
#: does overflow rather than against the luck the wide layout is currently enjoying.
OVERFLOWING_CHUNKS: dict[Aspect, tuple[str, ...]] = {
    Aspect.VERTICAL: (
        "version number, allowing",
        "modern computing hardware.",
        "several representations—plain",
        "instantly reachable but",
    ),
    Aspect.WIDE: (
        "incomprehensible synchronization representations compatibility interoperability",
    ),
}


def _wrap_mode(text: str) -> int:
    line = next(line for line in text.splitlines() if line.startswith("WrapStyle:"))
    return int(line.split(":", 1)[1])


def _dialogue_bodies(text: str) -> list[str]:
    return [_body(line) for line in text.splitlines() if line.startswith("Dialogue:")]


def _drawn_lines(body: str, font, box: float, mode: int) -> list[str]:
    """The lines libass draws for one Dialogue body, as an upper bound on width.

    A no-wrap mode draws the whole chunk on one line however wide it is — that is
    what put letters outside the frame. Every wrapping mode breaks on spaces, and
    greedy wrapping is the widest any of them gets: smart wrapping (0 and 3) only
    ever moves a word *down* from the greedy result, so measuring greedy can never
    let a real overflow through.
    """
    if mode not in WRAPPING_MODES:
        return [body]
    lines: list[str] = []
    current = ""
    for word in body.split():
        candidate = f"{current} {word}" if current else word
        if current and font.getlength(candidate) > box:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _chunked(phrases):
    """One word group per phrase, so each phrase stays a single caption chunk."""
    groups = []
    at = 0.0
    for phrase in phrases:
        group = []
        for word in phrase.split():
            group.append(w(word, at, at + 0.4))
            at += 0.4
        groups.append(group)
        at += 1.0
    return groups


@pytest.mark.parametrize("aspect", [Aspect.WIDE, Aspect.VERTICAL])
def test_no_caption_line_is_drawn_wider_than_its_own_text_box(tmp_path, caption_face, aspect):
    """The defect, in the units it was measured in: rendered pixels of type."""
    style = STYLES[aspect]
    font = caption_face(style)
    width, _ = PLAY_RES[aspect]
    box = width - style.margin_l - style.margin_r

    out = write_ass([], style, tmp_path / f"{aspect.value}.ass", play_res=PLAY_RES[aspect],
                    groups=_chunked(OVERFLOWING_CHUNKS[aspect]))
    text = out.read_text()
    bodies = _dialogue_bodies(text)

    assert bodies, "the fixture wrote no captions at all"
    # The premise: these chunks genuinely cannot be set on one line in this box.
    assert any(font.getlength(body) > box for body in bodies), "fixture no longer overflows"

    for body in bodies:
        for line in _drawn_lines(body, font, box, _wrap_mode(text)):
            drawn = font.getlength(line)
            assert drawn <= box, (
                f"{aspect.value}: {line!r} draws {drawn:.0f}px in a {box}px box"
                f" ({width}px frame, WrapStyle {_wrap_mode(text)})"
            )


@pytest.mark.parametrize("aspect", [Aspect.WIDE, Aspect.VERTICAL])
def test_the_style_line_carries_this_aspects_own_side_margins(tmp_path, aspect):
    """MarginL/MarginR come from the style, never from a literal in the writer."""
    style = STYLES[aspect]
    out = write_ass([w("hello", 0.0, 0.5)], style, tmp_path / f"{aspect.value}.ass",
                    play_res=PLAY_RES[aspect])
    fields = next(
        line.removeprefix("Style: ").split(",")
        for line in out.read_text().splitlines()
        if line.startswith("Style: ")
    )
    assert (fields[19], fields[20]) == (str(style.margin_l), str(style.margin_r))


def test_an_em_dash_inside_a_word_is_a_place_libass_may_break(tmp_path):
    """libass breaks on spaces and nothing else — not on a dash, not on U+200B.

    `representations—plain` is one 1055 px token in a 936 px box, so no wrap mode
    can save it. Measured against real libass in
    `tests/integration/test_caption_frame_fit.py`; the writer's half is here.
    """
    out = write_ass([w("representations—plain", 0.0, 0.5)], STYLES[Aspect.VERTICAL],
                    tmp_path / "d.ass", play_res=PLAY_RES[Aspect.VERTICAL])
    body = _dialogue_bodies(out.read_text())[0]
    assert body == "representations— plain"
    assert max(len(token) for token in body.split()) < len("representations—plain")


def test_a_dash_that_is_already_spaced_is_left_alone(tmp_path):
    out = write_ass([w("text", 0.0, 0.4), w("—", 0.4, 0.8), w("plain", 0.8, 1.2)],
                    STYLES[Aspect.VERTICAL], tmp_path / "e.ass",
                    play_res=PLAY_RES[Aspect.VERTICAL])
    assert _dialogue_bodies(out.read_text())[0] == "text — plain"


def test_a_hyphen_never_becomes_a_break(tmp_path):
    """`error‑correcting` is one word and fits; splitting it would be wrong."""
    out = write_ass([w("error‑correcting", 0.0, 0.5)], STYLES[Aspect.VERTICAL],
                    tmp_path / "f.ass", play_res=PLAY_RES[Aspect.VERTICAL])
    assert _dialogue_bodies(out.read_text())[0] == "error‑correcting"
    out = write_ass([w("state-of-the-art", 0.0, 0.5)], STYLES[Aspect.VERTICAL],
                    tmp_path / "g.ass", play_res=PLAY_RES[Aspect.VERTICAL])
    assert _dialogue_bodies(out.read_text())[0] == "state-of-the-art"
