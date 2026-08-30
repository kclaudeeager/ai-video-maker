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


def snap_to_script(script_text: str, heard: list[WordTiming]) -> list[WordTiming]:
    """Return the script's words carrying the timings whisper measured.

    - `equal` blocks copy timings straight across.
    - `replace` blocks share the heard block's whole span across the script tokens,
      proportionally to their length in characters.
    - `delete` (a script word whisper never heard) gets a zero-width slot at the
      previous word's end.
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
            for index in range(i1, i2):
                snapped.append(
                    WordTiming(word=script_tokens[index], start_s=cursor, end_s=cursor)
                )
        else:  # "replace": the heard block's span, split by script token length
            span_start = max(heard[j1].start_s, cursor)
            span_end = max(heard[j2 - 1].end_s, span_start)
            # A token that normalises away (pure punctuation) still gets a share.
            weights = [max(len(script_keys[index]), 1) for index in range(i1, i2)]
            total = sum(weights)
            span = span_end - span_start
            start_s = span_start
            for offset, index in enumerate(range(i1, i2)):
                last = offset == len(weights) - 1
                # The final token lands exactly on the span end: no drift, no gap.
                end_s = span_end if last else max(start_s + span * weights[offset] / total, start_s)
                snapped.append(WordTiming(word=script_tokens[index], start_s=start_s, end_s=end_s))
                start_s = end_s
            cursor = span_end

    return snapped
