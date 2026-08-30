from videomaker.align import snap_to_script
from videomaker.models import WordTiming


def w(word, start, end): return WordTiming(word=word, start_s=start, end_s=end)


def test_exact_match_passes_timings_through():
    heard = [w("hello", 0.0, 0.5), w("world", 0.5, 1.0)]
    out = snap_to_script("hello world", heard)
    assert [x.word for x in out] == ["hello", "world"]
    assert out[1].start_s == 0.5


def test_misheard_word_keeps_script_spelling():
    heard = [w("this", 0.0, 0.2), w("is", 0.2, 0.3), w("Kakoro", 0.3, 0.9)]
    out = snap_to_script("This is Kokoro", heard)
    assert [x.word for x in out] == ["This", "is", "Kokoro"]
    assert out[2].start_s == 0.3 and out[2].end_s == 0.9


def test_punctuation_and_case_do_not_break_alignment():
    heard = [w("hello", 0.0, 0.4), w("world", 0.4, 0.9)]
    out = snap_to_script("Hello, world!", heard)
    assert [x.word for x in out] == ["Hello,", "world!"]


def test_script_word_never_heard_gets_zero_width_slot():
    heard = [w("alpha", 0.0, 0.4), w("gamma", 0.4, 0.8)]
    out = snap_to_script("alpha beta gamma", heard)
    assert [x.word for x in out] == ["alpha", "beta", "gamma"]
    assert out[1].start_s == out[1].end_s == 0.4


def test_extra_heard_word_is_dropped():
    heard = [w("alpha", 0.0, 0.3), w("um", 0.3, 0.4), w("beta", 0.4, 0.8)]
    out = snap_to_script("alpha beta", heard)
    assert [x.word for x in out] == ["alpha", "beta"]


def test_many_to_one_replacement_splits_span_proportionally():
    heard = [w("gigabyte", 0.0, 1.0)]
    out = snap_to_script("giga byte", heard)
    assert len(out) == 2
    assert out[0].start_s == 0.0 and out[1].end_s == 1.0
    assert out[0].end_s == out[1].start_s


def test_output_is_always_monotonic():
    heard = [w("a", 0.0, 0.5), w("b", 0.5, 0.6), w("c", 0.6, 2.0)]
    out = snap_to_script("x y z w", heard)
    for prev, nxt in zip(out, out[1:]):  # noqa: RUF007  (plan text, kept verbatim)
        assert prev.end_s <= nxt.start_s + 1e-9


def test_empty_transcription_still_returns_one_slot_per_word():
    out = snap_to_script("alpha beta", [])
    assert [x.word for x in out] == ["alpha", "beta"]


def test_leading_unmatched_run_is_interpolated_not_collapsed():
    # M1 defect: faster-whisper dropped scene 3's opening phrase, so the first
    # seven script words all came back as a `delete` block. Collapsing them onto
    # the running cursor (still 0.0) produced a zero-duration caption and left the
    # following chunk on screen ~2 s ahead of the narration.
    heard = [w("so", 2.0, 2.4), w("the", 2.4, 2.6)]
    out = snap_to_script("Hard disks store data on rotating platters, so the", heard)

    assert [x.word for x in out] == [
        "Hard", "disks", "store", "data", "on", "rotating", "platters,", "so", "the",
    ]
    leading = out[:7]
    assert all(x.end_s > x.start_s for x in leading), "every dropped word needs a real slot"
    assert leading[0].start_s == 0.0
    assert len({x.start_s for x in leading}) == 7, "starts must be distinct, not all 0.0"
    assert [x.start_s for x in leading] == sorted(x.start_s for x in leading)
    # The run fills the gap up to the first word whisper actually heard.
    assert leading[-1].end_s == 2.0
    assert out[7].start_s == 2.0


def test_leading_unmatched_run_is_split_by_word_length():
    # Same proportional-to-character-length split the `replace` branch uses, so a
    # long word gets more of the span than a short one.
    heard = [w("end", 4.0, 4.5)]
    out = snap_to_script("a abc end", heard)
    assert out[0].end_s == out[1].start_s
    assert (out[1].end_s - out[1].start_s) > (out[0].end_s - out[0].start_s)
    assert out[1].end_s == 4.0


def test_mid_utterance_unmatched_run_is_interpolated():
    heard = [w("alpha", 0.0, 0.5), w("omega", 3.5, 4.0)]
    out = snap_to_script("alpha beta gamma delta omega", heard)
    middle = out[1:4]
    assert all(x.end_s > x.start_s for x in middle)
    assert middle[0].start_s == 0.5
    assert middle[-1].end_s == 3.5


def test_trailing_unmatched_run_stays_zero_width_without_an_anchor():
    # Nothing later to interpolate into, so the old behaviour is still correct.
    heard = [w("alpha", 0.0, 0.5)]
    out = snap_to_script("alpha beta gamma", heard)
    assert out[1].start_s == out[1].end_s == 0.5
    assert out[2].start_s == out[2].end_s == 0.5


def test_trailing_unmatched_run_uses_the_audio_duration_when_known():
    heard = [w("alpha", 0.0, 0.5)]
    out = snap_to_script("alpha beta gamma", heard, audio_duration_s=2.5)
    assert out[1].start_s == 0.5
    assert out[1].end_s == out[2].start_s
    assert out[2].end_s == 2.5


def test_interpolated_run_still_yields_one_monotonic_slot_per_token():
    heard = [w("mid", 1.0, 1.2)]
    out = snap_to_script("one two mid four five", heard, audio_duration_s=3.0)
    assert len(out) == 5
    for prev, nxt in zip(out, out[1:]):  # noqa: RUF007  (mirrors the test above)
        assert prev.end_s <= nxt.start_s + 1e-9
