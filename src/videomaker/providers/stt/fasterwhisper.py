"""Forced alignment with faster-whisper: local, CPU-only int8, no quota.

`faster_whisper` is imported inside the methods so the CLI and the mock pipeline
work without the optional `ml` extra installed.
"""

import os
from pathlib import Path

from videomaker.config import Settings
from videomaker.models import WordTiming
from videomaker.providers import register
from videomaker.providers.base import STTProvider
from videomaker.providers.errors import ProviderConfigError, ProviderError

PROVIDER_NAME = "fasterwhisper"
MODEL_SIZE = "base"  # M0: RTF ~1.02 warm on this machine; larger sizes are not worth it
DEVICE = "cpu"
COMPUTE_TYPE = "int8"  # no PyTorch, no CUDA — global constraint
WHISPER_CACHE_DIRNAME = "whisper"


def _hf_token(settings: Settings) -> str | None:
    """Optional Hugging Face token for the model download (M0 finding 9).

    Anonymous Hub downloads are rate-limited, so a token is honoured when present —
    from settings if the config grows a field for it, else from the environment.
    """
    token = str(getattr(settings, "hf_token", "") or "") or os.environ.get("HF_TOKEN", "")
    return token or None


@register("stt", PROVIDER_NAME)
class FasterWhisperSTT(STTProvider):
    """Word-level timings from `faster-whisper`.

    The `WhisperModel` is built lazily and then held as an instance attribute:
    construction dominates STT wall time (M0 finding 2), so building one per scene
    would make alignment cost realtime.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._model = None

    def _whisper(self):
        """The one `WhisperModel`, built on first use and reused thereafter."""
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:  # pragma: no cover - depends on the `ml` extra
                raise ProviderConfigError(
                    "faster-whisper is not installed; `uv sync --extra ml`"
                ) from exc
            self._model = WhisperModel(
                MODEL_SIZE,
                device=DEVICE,
                compute_type=COMPUTE_TYPE,
                download_root=str(self.settings.models_dir / WHISPER_CACHE_DIRNAME),
                use_auth_token=_hf_token(self.settings),
            )
        return self._model

    def transcribe_words(
        self,
        *,
        audio_path: Path,
        language: str = "en",
        hint_text: str = "",
    ) -> list[WordTiming]:
        path = Path(audio_path)
        try:
            # `initial_prompt` is the script: it materially improves proper-noun
            # timing, though whisper still spells them its own way — see
            # `videomaker.align.snap_to_script`.
            segments, _info = self._whisper().transcribe(
                str(path),
                language=language,
                word_timestamps=True,
                initial_prompt=hint_text or None,
            )
            timings: list[WordTiming] = []
            for segment in segments:  # a generator: transcription happens here
                for word in segment.words or []:
                    start_s = float(word.start)
                    end_s = max(float(word.end), start_s)
                    timings.append(
                        WordTiming(word=word.word.strip(), start_s=start_s, end_s=end_s)
                    )
        except OSError as exc:
            raise ProviderError(f"could not read {path} for alignment: {exc}") from exc
        return timings
