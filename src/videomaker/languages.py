"""Which languages this pipeline will actually produce a watchable video in.

**This is a recorded measurement, not a capability table copied from a README.**
Every verdict below comes from the round-trip spot check written up in
`docs/language-support.md`: synthesise one sentence with the language's own
Kokoro voice, transcribe it back with the same faster-whisper model the `align`
stage uses, and compare. A language is offered only when the sentence survived
the trip, because caption timing comes from that transcription — a take Whisper
mishears is a video whose captions are wrong, not merely late.

The narrowest link is **not** the caption font, which is what the M3 plan
predicted. libass falls back per glyph through fontconfig, so on a machine with
Noto installed all nine Kokoro languages *draw* correctly (verified by rendering
a frame and looking at it). The links that actually broke were the TTS
phonemiser and the STT model — see the `note` on each held-back language.

Three fields carry three different code systems, and they disagree on purpose:

* `code` is what `Project.language` stores and what faster-whisper is given —
  ISO 639-1, which is the only one of the three the STT model accepts;
* `espeak` is what Kokoro is given, because it phonemises through espeak-ng and
  espeak speaks its own dialect codes (`fr-fr`, `pt-br`, `cmn`);
* `script_sample` is what the font probe in `media.fonts` asks about — the
  characters this language cannot be written without.

Nothing here imports from the rest of the package: `project`, `doctor`,
`media.fonts` and the web layer all read it, and it must not drag a provider
import into any of them.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Language:
    """One language, and whether the whole chain was measured to work in it."""

    #: ISO 639-1. Stored in `Project.language`; handed to faster-whisper as-is.
    code: str
    #: How it is written in the UI.
    name: str
    #: espeak-ng's own code, which Kokoro phonemises with.
    espeak: str
    #: Characters captions in this language cannot avoid. The font probe asks
    #: about exactly these, so they must be *representative*, not exhaustive:
    #: a script whose sample draws, draws.
    script_sample: str
    #: Measured end to end, and it worked.
    offered: bool
    #: Why not, when `offered` is false. Empty otherwise.
    note: str = ""


#: The em dash and curly quotes are in the English sample deliberately: the LLM
#: writes them whether or not the font has them, and a caption font without them
#: fails on English text that looks like plain ASCII in the script editor.
_ENGLISH_SAMPLE = "Aa—“”’"

#: Every language Kokoro has voices for, offered or not. Keyed by ISO 639-1 and
#: ordered as the menu shows them: the offered ones first, in menu order.
LANGUAGES: dict[str, Language] = {
    "en": Language(
        code="en",
        name="English",
        espeak="en-us",
        script_sample=_ENGLISH_SAMPLE,
        offered=True,
    ),
    "es": Language(
        code="es",
        name="Spanish",
        espeak="es",
        script_sample="áéíóúüñÑ¿¡",
        offered=True,
    ),
    "fr": Language(
        code="fr",
        name="French",
        espeak="fr-fr",
        script_sample="àâçéèêëîïôùûüœ’",
        offered=True,
    ),
    "it": Language(
        code="it",
        name="Italian",
        espeak="it",
        script_sample="àèéìòù",
        offered=True,
    ),
    "pt": Language(
        code="pt",
        name="Portuguese",
        espeak="pt-br",
        script_sample="ãõáâàçéêíóôú",
        offered=True,
    ),
    "hi": Language(
        code="hi",
        name="Hindi",
        espeak="hi",
        script_sample="सॉलिडस्टेटमरन",
        offered=False,
        note=(
            "Kokoro speaks it, but faster-whisper returned the take in Urdu script "
            "even with language='hi', so caption timings cannot be snapped back onto "
            "the Devanagari script. Measured 2026-08-31."
        ),
    ),
    "ja": Language(
        code="ja",
        name="Japanese",
        espeak="ja",
        script_sample="日本語あイ",
        offered=False,
        note=(
            "Kokoro's Japanese voices were trained on misaki[ja] phonemes, not "
            "espeak's. Through espeak the take runs four times too long and says the "
            "English word 'Japanese' out loud. Measured 2026-08-31."
        ),
    ),
    "zh": Language(
        code="zh",
        name="Chinese",
        espeak="cmn",
        script_sample="中文汉字，",
        offered=False,
        note=(
            "espeak's Mandarin phonemes lose tone in Kokoro's vocabulary, and the "
            "round trip came back as mostly the wrong characters. The font is fine — "
            "the audio is not. Measured 2026-08-31."
        ),
    ),
}

#: The languages the measurement cleared, in menu order.
OFFERED_CODES: tuple[str, ...] = tuple(code for code, lang in LANGUAGES.items() if lang.offered)


def language_for(code: str) -> Language | None:
    """The registry entry for `code`, or None — never a KeyError.

    A project written by an older build, or hand-edited, can carry any string at
    all in `language`; the callers are a form and a doctor check, both of which
    would rather say "unknown" than raise.
    """
    return LANGUAGES.get(code)
