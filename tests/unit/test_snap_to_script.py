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
