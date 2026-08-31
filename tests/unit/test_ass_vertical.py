"""The vertical caption layout and karaoke word-pop.

Two things are load-bearing and both burned this codebase once already:

* ASS colours are ``&HAABBGGRR`` — **BGR**. A highlight constant written in RGB
  order renders a different colour with no error anywhere, so the byte order of
  the karaoke highlight is asserted as a literal here.
* Whisper word timings are zero-gap and can be degenerate (``start == end ==
  0.0``). Karaoke multiplies the number of ``Dialogue`` events by the number of
  words, so every one of them is re-checked for ``end > start``.

The vertical style itself is **authored**, never derived from the wide numbers
(spec 4.5). The tests below assert its literal values, so an accidental
`STYLES[WIDE].font_size * 1.5` fails here rather than drifting silently the next
time the wide layout is tuned.
"""

from itertools import pairwise

import pytest

from videomaker.media.ass import (
    KARAOKE_HIGHLIGHT_COLOUR,
    MIN_DISPLAY_DURATION_S,
    MIN_KARAOKE_SPAN_S,
    STYLES,
    ass_colour,
    karaoke_spans,
    override_colour,
    write_ass,
)
from videomaker.models import Aspect, WordTiming

VERTICAL_PLAY_RES = (1080, 1920)


def w(word, s, e):
    return WordTiming(word=word, start_s=s, end_s=e)


def _dialogues(text):
    return [line for line in text.splitlines() if line.startswith("Dialogue:")]


def _seconds(stamp):
    hours, minutes, rest = stamp.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(rest)


def _spans(text):
    spans = []
    for line in _dialogues(text):
        _, start, end, *_ = line[len("Dialogue:") :].split(",")
        spans.append((_seconds(start), _seconds(end)))
    return spans


def _body(line):
    """The Text field of a ``Dialogue:`` line — everything after the 9th comma."""
    return line[len("Dialogue:") :].split(",", 9)[9]


# ------------------------------------------------------------------ the style


def test_vertical_style_exists_with_the_authored_literals():
    style = STYLES[Aspect.VERTICAL]
    assert style.font_size == 96
    assert style.words_per_chunk == 3


def test_vertical_text_is_centred_at_about_62_percent_of_frame_height():
    """Spec 4.5: vertical captions sit centred at ~62% height, not lower-third."""
    style = STYLES[Aspect.VERTICAL]
    assert style.alignment == 2  # bottom-centre: MarginV measures up from the bottom
    _, height = VERTICAL_PLAY_RES
    line_height = style.font_size * 1.2  # libass' default line advance
    centre = height - style.margin_v - line_height / 2
    assert 0.60 <= centre / height <= 0.64, f"text centre at {centre / height:.3f} of height"


def test_vertical_style_is_authored_not_scaled_from_wide():
    wide = STYLES[Aspect.WIDE]
    vertical = STYLES[Aspect.VERTICAL]
    # Nothing about vertical is a clean multiple of wide: if someone replaces the
    # authored numbers with arithmetic on the wide ones, the ratios line up and this
    # fails. (font 96/64 == 1.5 is a coincidence of the spec; the rest must not match.)
    assert vertical.margin_v != wide.margin_v
    assert vertical.words_per_chunk != wide.words_per_chunk
    assert vertical.outline != wide.outline


def test_the_wide_style_is_untouched():
    """Every rendered project's cached wide fingerprint depends on these numbers."""
    wide = STYLES[Aspect.WIDE]
    assert (wide.font_size, wide.words_per_chunk, wide.margin_v, wide.alignment) == (
        64,
        5,
        160,
        2,
    )
    assert (wide.outline, wide.shadow) == (3.0, 1.0)


def test_vertical_header_is_written_at_the_vertical_frame(tmp_path):
    out = write_ass(
        [w("hello", 0.0, 0.5)],
        STYLES[Aspect.VERTICAL],
        tmp_path / "v.ass",
        play_res=VERTICAL_PLAY_RES,
    )
    text = out.read_text()
    assert "PlayResX: 1080" in text and "PlayResY: 1920" in text
    assert ",96," in text  # the authored font size reached the style line


def test_vertical_chunks_are_three_words(tmp_path):
    words = [w(f"w{i}", i * 0.4, (i + 1) * 0.4) for i in range(6)]
    out = write_ass(words, STYLES[Aspect.VERTICAL], tmp_path / "v.ass",
                    play_res=VERTICAL_PLAY_RES)
    assert len(_dialogues(out.read_text())) == 2


# --------------------------------------------------------------- trap 1: BGR


def test_karaoke_highlight_colour_is_bgr_not_rgb():
    # The highlight is amber #FFD60A. In ASS that is &H00 + BB GG RR = 0A D6 FF.
    assert KARAOKE_HIGHLIGHT_COLOUR == "&H000AD6FF"
    assert KARAOKE_HIGHLIGHT_COLOUR != "&H00FFD60A"  # the RGB-ordered mistake
    assert KARAOKE_HIGHLIGHT_COLOUR == ass_colour(255, 214, 10)


def test_override_colour_keeps_the_bgr_bytes_and_drops_the_alpha():
    assert override_colour(ass_colour(255, 0, 0)) == "&H0000FF&"  # red, BGR
    assert override_colour(KARAOKE_HIGHLIGHT_COLOUR) == "&H0AD6FF&"


def test_karaoke_line_recolours_only_the_active_word(tmp_path):
    words = [w("alpha", 0.0, 0.5), w("beta", 0.5, 1.0), w("gamma", 1.0, 1.5)]
    out = write_ass(words, STYLES[Aspect.VERTICAL], tmp_path / "v.ass",
                    play_res=VERTICAL_PLAY_RES, karaoke=True)
    lines = _dialogues(out.read_text())
    assert len(lines) == 3  # one event per word
    highlight = f"{{\\1c{override_colour(KARAOKE_HIGHLIGHT_COLOUR)}}}"
    reset = f"{{\\1c{override_colour(STYLES[Aspect.VERTICAL].primary_colour)}}}"
    for index, expected in enumerate(("alpha", "beta", "gamma")):
        body = _body(lines[index])
        assert body.count(highlight) == 1, body
        assert f"{highlight}{expected}{reset}" in body, body
        # ...and the whole chunk is still on screen behind it.
        for word in ("alpha", "beta", "gamma"):
            assert word in body


def test_karaoke_is_off_by_default(tmp_path):
    words = [w("alpha", 0.0, 0.5), w("beta", 0.5, 1.0), w("gamma", 1.0, 1.5)]
    out = write_ass(words, STYLES[Aspect.VERTICAL], tmp_path / "v.ass",
                    play_res=VERTICAL_PLAY_RES)
    text = out.read_text()
    assert len(_dialogues(text)) == 1
    assert override_colour(KARAOKE_HIGHLIGHT_COLOUR) not in text


def test_karaoke_events_tile_the_chunk_window_without_gaps(tmp_path):
    words = [w("alpha", 0.0, 0.5), w("beta", 0.5, 1.0), w("gamma", 1.0, 1.5)]
    out = write_ass(words, STYLES[Aspect.VERTICAL], tmp_path / "v.ass",
                    play_res=VERTICAL_PLAY_RES, karaoke=True)
    spans = _spans(out.read_text())
    assert spans[0][0] == pytest.approx(0.0, abs=0.01)
    for (_, end), (next_start, _) in pairwise(spans):
        assert next_start == pytest.approx(end, abs=0.01), "a gap unhighlights the caption"


# -------------------------------------------------- trap 2: degenerate timings

# The exact M1 shape: whisper returned start == end == 0.0 for a run of words.
DEGENERATE = [w(f"word{i}", 0.0, 0.0) for i in range(7)] + [
    w("so", 2.08, 2.70),
    w("the", 2.70, 2.94),
    w("read", 2.94, 3.40),
]


def test_no_karaoke_event_has_end_at_or_before_start(tmp_path):
    out = write_ass(DEGENERATE, STYLES[Aspect.VERTICAL], tmp_path / "v.ass",
                    play_res=VERTICAL_PLAY_RES, karaoke=True)
    spans = _spans(out.read_text())
    assert spans, "degenerate timings must not silence the whole file"
    for start, end in spans:
        assert end > start, f"non-displaying karaoke event {start} -> {end}"


def test_no_karaoke_event_has_end_at_or_before_start_when_grouped(tmp_path):
    groups = [DEGENERATE, [w("next", 5.0, 5.4), w("scene", 5.4, 5.9)]]
    out = write_ass([], STYLES[Aspect.VERTICAL], tmp_path / "v.ass",
                    play_res=VERTICAL_PLAY_RES, karaoke=True, groups=groups)
    spans = _spans(out.read_text())
    assert spans
    for start, end in spans:
        assert end > start


def test_karaoke_keeps_every_degenerate_word_somewhere(tmp_path):
    out = write_ass(DEGENERATE, STYLES[Aspect.VERTICAL], tmp_path / "v.ass",
                    play_res=VERTICAL_PLAY_RES, karaoke=True)
    text = out.read_text()
    for word in DEGENERATE:
        assert word.word in text


def test_every_karaoke_event_is_at_least_one_visible_span(tmp_path):
    out = write_ass(DEGENERATE, STYLES[Aspect.VERTICAL], tmp_path / "v.ass",
                    play_res=VERTICAL_PLAY_RES, karaoke=True)
    for start, end in _spans(out.read_text()):
        assert end - start >= MIN_KARAOKE_SPAN_S - 0.005


def test_all_words_degenerate_still_emits_a_displayable_line(tmp_path):
    words = [w(f"w{i}", 0.0, 0.0) for i in range(10)]
    out = write_ass(words, STYLES[Aspect.VERTICAL], tmp_path / "v.ass",
                    play_res=VERTICAL_PLAY_RES, karaoke=True)
    spans = _spans(out.read_text())
    assert spans
    for start, end in spans:
        assert end > start
        assert end - start >= MIN_DISPLAY_DURATION_S or end - start >= MIN_KARAOKE_SPAN_S


def test_karaoke_spans_fall_back_to_even_division_when_word_starts_collide():
    chunk = [w("a", 0.0, 0.0), w("b", 0.0, 0.0), w("c", 0.0, 0.9)]
    spans = karaoke_spans(chunk)
    assert [round(s, 3) for s, _ in spans] == [0.0, 0.3, 0.6]
    for start, end in spans:
        assert end > start


def test_karaoke_spans_refuse_a_window_too_short_to_split():
    # 0.16s across 3 words is 0.053s each — under a couple of frames. Highlighting
    # word by word there is a flicker, so the caller must fall back to a plain line.
    chunk = [w("a", 0.0, 0.0), w("b", 0.0, 0.0), w("c", 0.0, 0.16)]
    assert karaoke_spans(chunk) == []


def test_a_chunk_too_short_to_karaoke_is_still_written_as_a_plain_line(tmp_path):
    # Three degenerate words clamped against a next chunk starting at 0.17s: the
    # window survives MIN_DISPLAY_DURATION_S but 0.17/3 is under MIN_KARAOKE_SPAN_S.
    words = [w("a", 0.0, 0.0), w("b", 0.0, 0.0), w("c", 0.0, 0.0)]
    words += [w("d", 0.17, 0.6), w("e", 0.6, 1.0), w("f", 1.0, 1.4)]
    out = write_ass(words, STYLES[Aspect.VERTICAL], tmp_path / "v.ass",
                    play_res=VERTICAL_PLAY_RES, karaoke=True)
    lines = _dialogues(out.read_text())
    body = _body(lines[0])
    assert body == "a b c", body  # no override tags, all three words present
    assert len(lines) == 1 + 3  # the plain line, then the next chunk word by word


# ------------------------------------------------ the escaping/override overlap


def test_karaoke_tags_are_not_escaped_but_narration_braces_still_are(tmp_path):
    words = [w("{drop}", 0.0, 0.5), w("safe", 0.5, 1.0), w("tail", 1.0, 1.5)]
    out = write_ass(words, STYLES[Aspect.VERTICAL], tmp_path / "v.ass",
                    play_res=VERTICAL_PLAY_RES, karaoke=True)
    body = _body(_dialogues(out.read_text())[0])
    # The narration's own braces stay escaped — unescaped they parse as a tag.
    assert "\\{drop\\}" in body
    # ...while the override block we emitted on purpose is raw.
    assert f"{{\\1c{override_colour(KARAOKE_HIGHLIGHT_COLOUR)}}}" in body
    assert "\\{\\1c" not in body
