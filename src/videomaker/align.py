"""Snap a whisper transcription back onto the script that was spoken.

Whisper hears its own spelling — in the M0 spike it transcribed "Kokoro" as
"Kakoro" — and it drops or invents the odd word. Captions must show the *script's*
words (the writer's spelling, capitalisation and punctuation) carrying *whisper's*
timings, so the two token streams are diffed with `difflib` and the timings are
transplanted across the diff.

The result always has exactly one entry per script token, in script order, with
monotonically non-decreasing times. Downstream stages (captions, assembly) rely on
that invariant and never re-check it.
"""

from difflib import SequenceMatcher

from videomaker.models import WordTiming

__all__ = ["snap_to_script", "tokenize"]


def tokenize(text: str) -> list[str]:
    """Script tokens exactly as written — whitespace-split, nothing stripped."""
    return text.split()


def _normalise(token: str) -> str:
    """Matching key for a token: case- and punctuation-insensitive.

    Used only to line the two streams up; the emitted words keep their original
    spelling. Dropping every non-alphanumeric character (rather than only stripping
    the edges) makes "don't"/"dont" and "Kokoro—" match their bare forms.
    """
    return "".join(ch for ch in token.lower() if ch.isalnum())


def _spread(
    tokens: list[str],
    keys: list[str],
    span_start: float,
    span_end: float,
) -> list[WordTiming]:
    """Lay ``tokens`` end to end across ``[span_start, span_end]``.

    Each token's share of the span is proportional to its length in characters, and
    the last token lands exactly on ``span_end`` so the run neither drifts nor leaves
    a gap before whatever follows. A zero-width span yields zero-width slots, which
    is the only honest answer when there is no room to interpolate into.
    """
    # A token that normalises away (pure punctuation) still gets a share.
    weights = [max(len(key), 1) for key in keys]
    total = sum(weights)
    span = max(span_end - span_start, 0.0)

    out: list[WordTiming] = []
    start_s = span_start
    for offset, token in enumerate(tokens):
        last = offset == len(tokens) - 1
        end_s = span_end if last else max(start_s + span * weights[offset] / total, start_s)
        out.append(WordTiming(word=token, start_s=start_s, end_s=end_s))
        start_s = end_s
    return out


def snap_to_script(
    script_text: str,
    heard: list[WordTiming],
    *,
    audio_duration_s: float | None = None,
) -> list[WordTiming]:
    """Return the script's words carrying the timings whisper measured.

    - `equal` blocks copy timings straight across.
    - `replace` blocks share the heard block's whole span across the script tokens,
      proportionally to their length in characters.
    - `delete` (a run of script words whisper never heard) is *interpolated* across
      the gap it sits in — from the previous word's end to the next heard word's
      start — using the same proportional split. Collapsing such a run onto a single
      instant is what produced M1's zero-duration caption and left the following
      chunk running ~2 s ahead of the narration (spike follow-up 1). A trailing run
      has no later anchor, so it falls back to ``audio_duration_s`` when the caller
      knows it and stays zero-width otherwise.
    - `insert` (a word whisper heard that is not in the script) is dropped.
    """
    script_tokens = tokenize(script_text)
    if not script_tokens:
        return []

    script_keys = [_normalise(token) for token in script_tokens]
    heard_keys = [_normalise(timing.word) for timing in heard]
    matcher = SequenceMatcher(a=script_keys, b=heard_keys, autojunk=False)

    snapped: list[WordTiming] = []
    cursor = 0.0  # end of the last slot emitted; nothing may start before it

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "insert":
            continue
        if tag == "equal":
            for offset in range(i2 - i1):
                timing = heard[j1 + offset]
                start_s = max(timing.start_s, cursor)
                end_s = max(timing.end_s, start_s)
                snapped.append(
                    WordTiming(word=script_tokens[i1 + offset], start_s=start_s, end_s=end_s)
                )
                cursor = end_s
        elif tag == "delete":
            # The next word whisper *did* hear bounds the run. `j1 == j2` here, so
            # `heard[j1]` is whatever comes next in the transcription; if the run is
            # trailing there is nothing after it but the end of the audio.
            if j1 < len(heard):
                anchor = heard[j1].start_s
            elif audio_duration_s is not None:
                anchor = audio_duration_s
            else:
                anchor = cursor
            span_end = max(anchor, cursor)
            snapped.extend(_spread(script_tokens[i1:i2], script_keys[i1:i2], cursor, span_end))
            cursor = span_end
        else:  # "replace": the heard block's span, split by script token length
            span_start = max(heard[j1].start_s, cursor)
            span_end = max(heard[j2 - 1].end_s, span_start)
            snapped.extend(
                _spread(script_tokens[i1:i2], script_keys[i1:i2], span_start, span_end)
            )
            cursor = span_end

    return snapped
