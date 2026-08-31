"""Narration with kokoro-onnx: local, CPU-only, no quota, no network.

`kokoro_onnx` is imported inside the methods, never at module scope, so the CLI
and the mock pipeline keep working without the optional `ml` extra installed.
"""

from dataclasses import dataclass
from pathlib import Path

from videomaker.config import Settings
from videomaker.providers import register
from videomaker.providers.base import TTSProvider, TTSResult
from videomaker.providers.errors import ProviderConfigError, ProviderError

PROVIDER_NAME = "kokoro"
KOKORO_MODEL_FILE = "kokoro-v1.0.onnx"
KOKORO_VOICES_FILE = "voices-v1.0.bin"
SETUP_HINT = "run `uv run videomaker setup` to download the Kokoro model files"

# Kokoro speaks espeak-ng language codes; the pipeline speaks ISO ones.
LANGUAGE_ALIASES = {"en": "en-us", "en-gb": "en-gb", "en-us": "en-us"}

# M0 finding 4: Kokoro emits float32 @ 24 kHz mono. `soundfile.write` downcasts
# to 16-bit PCM silently, so the subtype is stated rather than inherited.
WAV_SUBTYPE = "PCM_16"

# ------------------------------------------------------------------ voice naming
#
# Kokoro names every voice `<lang><gender>_<name>`: `af_heart` is American
# English, female, "Heart"; `bm_george` is British English, male, "George". The
# tables live here rather than in the web layer because this module is the one
# that owns the convention — the model file is where the ids come from.

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


@dataclass(frozen=True)
class VoiceInfo:
    """One voice id, read as the three things a person picking a voice wants."""

    id: str
    language: str
    gender: str
    display_name: str


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
    gender = VOICE_GENDERS.get(prefix[1:2], UNKNOWN_GENDER) if known else UNKNOWN_GENDER
    display = (name if known else voice_id).replace("_", " ").strip().title()
    return VoiceInfo(
        id=voice_id,
        language=language,
        gender=gender,
        display_name=display or voice_id,
    )


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
                raise ProviderConfigError(
                    "kokoro-onnx is not installed; `uv sync --extra ml`"
                ) from exc
            model, voices = self._model_paths()
            self._engine = Kokoro(str(model), str(voices))
        return self._engine

    def voices(self) -> list[str]:
        return sorted(self._kokoro().get_voices())

    def synthesize(
        self,
        *,
        text: str,
        voice: str,
        out_path: Path,
        speed: float = 1.0,
        language: str = "en",
    ) -> TTSResult:
        import soundfile as sf

        spoken = text.strip()
        if not spoken:
            raise ProviderError("cannot synthesize an empty narration")
        kokoro = self._kokoro()
        lang = LANGUAGE_ALIASES.get(language.lower(), language)
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
