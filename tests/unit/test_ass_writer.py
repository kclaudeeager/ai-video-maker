from videomaker.media.ass import (
    MIN_DISPLAY_DURATION_S,
    STYLES,
    ass_colour,
    chunk_words,
    write_ass,
)
from videomaker.models import Aspect, WordTiming


def w(word, s, e): return WordTiming(word=word, start_s=s, end_s=e)


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
