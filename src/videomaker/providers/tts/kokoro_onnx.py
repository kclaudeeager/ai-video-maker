"""Narration with kokoro-onnx: local, CPU-only, no quota, no network.

`kokoro_onnx` is imported inside the methods, never at module scope, so the CLI
and the mock pipeline keep working without the optional `ml` extra installed.
"""

from dataclasses import dataclass
from pathlib import Path

from videomaker.config import Settings
from videomaker.languages import language_for
from videomaker.providers import register
from videomaker.providers.base import TTSProvider, TTSResult
from videomaker.providers.errors import ProviderConfigError, ProviderError

PROVIDER_NAME = "kokoro"
KOKORO_MODEL_FILE = "kokoro-v1.0.onnx"
KOKORO_VOICES_FILE = "voices-v1.0.bin"
SETUP_HINT = "run `uv run videomaker setup` to download the Kokoro model files"
#: What to do about a missing optional dependency. One string, because two
#: providers and three imports were each phrasing it their own way.
EXTRA_HINT = "install the local-voice stack with `uv sync --extra ml`"

# Kokoro speaks espeak-ng language codes; the pipeline speaks ISO ones. The
# translation lives in `videomaker.languages`, which is also what the create form
# and `doctor` read, so there is one table rather than three that can drift.
# Only the English dialect split is decided here, because only the voice knows it:
# `Project.language` is `en` for both `af_heart` and `bm_george`, and espeak has
# a different accent for each.
ENGLISH_DIALECTS = {"a": "en-us", "b": "en-gb"}

# M0 finding 4: Kokoro emits float32 @ 24 kHz mono. `soundfile.write` downcasts
# to 16-bit PCM silently, so the subtype is stated rather than inherited.
WAV_SUBTYPE = "PCM_16"

# ------------------------------------------------------------------ voice naming
#
# Kokoro names every voice `<lang><gender>_<name>`: `af_heart` is American
# English, female, "Heart"; `bm_george` is British English, male, "George". The
# tables live here rather than in the web layer because this module is the one
# that owns the convention — the model file is where the ids come from.

#: First letter of a voice id -> the ISO 639-1 code the *pipeline* speaks: what
#: `Project.language` stores, what faster-whisper is handed, and the key into
#: `videomaker.languages`. This is the one fact that makes a separate language
#: control unnecessary — the prefix already is the answer.
VOICE_LANGUAGE_CODES: dict[str, str] = {
    "a": "en",
    "b": "en",
    "e": "es",
    "f": "fr",
    "h": "hi",
    "i": "it",
    "j": "ja",
    "p": "pt",
    "z": "zh",
}

#: First letter of a voice id -> the language it speaks, in words.
VOICE_LANGUAGES: dict[str, str] = {
    "a": "American English",
    "b": "British English",
    "e": "Spanish",
    "f": "French",
    "h": "Hindi",
    "i": "Italian",
    "j": "Japanese",
    "p": "Portuguese",
    "z": "Chinese",
}

#: Second letter of a voice id -> the voice's gender.
VOICE_GENDERS: dict[str, str] = {"f": "female", "m": "male"}

#: What an id outside the convention gets. A new Kokoro release may ship a voice
#: whose prefix is not in the tables above, and a menu that omits it — or a
#: `KeyError` from the create form — would be worse than an honest "Other".
UNKNOWN_LANGUAGE = "Other"
UNKNOWN_GENDER = "unspecified"

#: What an unreadable prefix yields for the *code*. Empty rather than `"en"`:
#: a caller deriving `Project.language` must be able to tell "no idea" from
#: "English", and guessing English for a voice we cannot read is how a French
#: script ends up aligned by an English model.
UNKNOWN_CODE = ""


@dataclass(frozen=True)
class VoiceInfo:
    """One voice id, read as the three things a person picking a voice wants."""

    id: str
    language: str
    gender: str
    display_name: str
    #: ISO 639-1, or `UNKNOWN_CODE`. `Project.language` is derived from this.
    code: str = UNKNOWN_CODE


def describe_voice(voice_id: str) -> VoiceInfo:
    """Read `voice_id` as Kokoro names it. Pure, total, and never raises.

    Every part degrades on its own: `qf_luna` keeps its known gender and loses
    only the language, and an id with no prefix at all still comes back with a
    readable name. The caller is a form that must render whatever the model file
    happens to contain.
    """
    prefix, _, name = voice_id.partition("_")
    known = len(prefix) == 2 and bool(name)
    language = VOICE_LANGUAGES.get(prefix[:1], UNKNOWN_LANGUAGE) if known else UNKNOWN_LANGUAGE
    code = VOICE_LANGUAGE_CODES.get(prefix[:1], UNKNOWN_CODE) if known else UNKNOWN_CODE
    gender = VOICE_GENDERS.get(prefix[1:2], UNKNOWN_GENDER) if known else UNKNOWN_GENDER
    display = (name if known else voice_id).replace("_", " ").strip().title()
    return VoiceInfo(
        id=voice_id,
        language=language,
        gender=gender,
        display_name=display or voice_id,
        code=code,
    )


def espeak_language(language: str, voice: str) -> str:
    """The espeak-ng code Kokoro should phonemise `voice` with, for `language`.

    Two translations in one place, because getting either wrong is silent: an
    unregistered code is passed through untouched (Kokoro may learn a language
    before this table does, and mangling it would be worse than forwarding it),
    and English resolves to the dialect the voice's own prefix names.
    """
    entry = language_for(language.lower())
    spoken = entry.espeak if entry is not None else language
    if entry is not None and entry.code == "en":
        return ENGLISH_DIALECTS.get(voice[:1], spoken)
    return spoken


def _soundfile():
    """`soundfile`, or the same refusal the engine import gives.

    Guarded for the reason every optional import in this package is guarded, and
    it was the one that was not. Two things went wrong because of that:

    * a bare `ModuleNotFoundError` is not a `ProviderError`, so it does not
      advance the chain — a run configured `tts: [kokoro, vendor]` died where it
      should have fallen through to the vendor;
    * it sat at the top of `synthesize`, *above* the `kokoro_onnx` import, so on
      a clone without the `ml` extra it pre-empted the friendly "kokoro-onnx is
      not installed" message. The worse of the two errors is the one that won,
      and the reader showed `No module named 'soundfile'`.
    """
    try:
        import soundfile as sf
    except ImportError as exc:  # pragma: no cover - depends on the `ml` extra
        raise ProviderConfigError(f"soundfile is not installed; {EXTRA_HINT}") from exc
    return sf


@register("tts", PROVIDER_NAME)
class KokoroTTS(TTSProvider):
    """Kokoro v1.0 ONNX narration.

    The `Kokoro` engine is built lazily and then held as an instance attribute:
    loading the 310 MB graph dominates synthesis wall time (M0 finding 2), and a
    project narrates one scene at a time against the same provider instance.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._engine = None

    def _model_paths(self) -> tuple[Path, Path]:
        model = self.settings.models_dir / KOKORO_MODEL_FILE
        voices = self.settings.models_dir / KOKORO_VOICES_FILE
        missing = [str(path) for path in (model, voices) if not path.is_file()]
        if missing:
            joined = ", ".join(missing)
            raise ProviderConfigError(f"missing Kokoro model files: {joined}; {SETUP_HINT}")
        return model, voices

    def _kokoro(self):
        """The one `Kokoro` instance, built on first use and reused thereafter."""
        if self._engine is None:
            try:
                from kokoro_onnx import Kokoro
            except ImportError as exc:  # pragma: no cover - depends on the `ml` extra
                raise ProviderConfigError(f"kokoro-onnx is not installed; {EXTRA_HINT}") from exc
            model, voices = self._model_paths()
            self._engine = Kokoro(str(model), str(voices))
        return self._engine

    def voices(self) -> list[str]:
        """The voices this provider can actually produce a file with.

        `_soundfile()` is checked here and not only in `synthesize`, and that is
        the point rather than belt-and-braces: `voices()` is what every caller
        asks to find out whether this provider is *usable*. The reader builds its
        mode table from it (`web/routes/library.spoken_languages`) and the create
        form builds its menu from it.

        With a half-installed `ml` extra — `kokoro_onnx` present, `soundfile`
        absent — listing succeeded and synthesis failed, so the reader offered
        Listen on a work it could not speak and the failure arrived after the
        button. `docs/multimodal-reader-design.md` §6 asks for the opposite: a
        mode that is absent rather than present and broken. Answering "no voices"
        is what makes that true, and it costs one import that is cached after the
        first call.
        """
        engine = self._kokoro()
        _soundfile()
        return sorted(engine.get_voices())

    def synthesize(
        self,
        *,
        text: str,
        voice: str,
        out_path: Path,
        speed: float = 1.0,
        language: str = "en",
    ) -> TTSResult:
        sf = _soundfile()
        spoken = text.strip()
        if not spoken:
            raise ProviderError("cannot synthesize an empty narration")
        kokoro = self._kokoro()
        lang = espeak_language(language, voice)
        try:
            samples, sample_rate = kokoro.create(spoken, voice=voice, speed=speed, lang=lang)
        except Exception as exc:  # kokoro raises bare ValueErrors for bad voice/lang
            raise ProviderError(f"Kokoro failed to synthesize: {exc}") from exc

        path = Path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(path), samples, sample_rate, subtype=WAV_SUBTYPE)
        # The true duration of what was written — never an estimate from word count.
        return TTSResult(
            path=path, duration_s=len(samples) / float(sample_rate), sample_rate=int(sample_rate)
        )
