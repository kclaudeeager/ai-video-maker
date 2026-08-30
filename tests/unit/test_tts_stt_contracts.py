"""Contract tests for the real Kokoro TTS and faster-whisper STT providers.

These need the optional ``ml`` extra (and, for the inference tests, the model
weights ``videomaker setup`` downloads), so the whole module skips cleanly when
either is missing — CI installs without ``ml``.
"""

from itertools import pairwise

import pytest

# `exc_type=ImportError` so a half-installed extra (kokoro present, its native
# runtime broken) skips too, rather than erroring the whole collection.
pytest.importorskip("kokoro_onnx", exc_type=ImportError)
pytest.importorskip("faster_whisper", exc_type=ImportError)
soundfile = pytest.importorskip("soundfile", exc_type=ImportError)

import faster_whisper
import kokoro_onnx

from videomaker.align import snap_to_script
from videomaker.config import Settings
from videomaker.providers import get_provider
from videomaker.providers.base import STTProvider, TTSProvider
from videomaker.providers.errors import ProviderConfigError
from videomaker.providers.stt.fasterwhisper import FasterWhisperSTT
from videomaker.providers.tts.kokoro_onnx import (
    KOKORO_MODEL_FILE,
    KOKORO_VOICES_FILE,
    KokoroTTS,
)

pytestmark = pytest.mark.slow

SCRIPT = "This is a Kokoro voice test on this machine."
SAMPLE_RATE = 24000


@pytest.fixture(scope="module")
def settings() -> Settings:
    s = Settings()
    for name in (KOKORO_MODEL_FILE, KOKORO_VOICES_FILE):
        if not (s.models_dir / name).is_file():
            pytest.skip(f"{name} not downloaded; run `uv run videomaker setup`")
    return s


@pytest.fixture(scope="module")
def narration(settings: Settings, tmp_path_factory: pytest.TempPathFactory):
    tts = get_provider("tts", "kokoro", settings)
    out_path = tmp_path_factory.mktemp("narration") / "narration.wav"
    return tts.synthesize(text=SCRIPT, voice="af_heart", out_path=out_path)


def test_providers_are_registered_under_their_plan_names(settings: Settings) -> None:
    assert isinstance(get_provider("tts", "kokoro", settings), TTSProvider)
    assert isinstance(get_provider("stt", "fasterwhisper", settings), STTProvider)


def test_missing_model_files_raise_a_config_error(tmp_path) -> None:
    tts = KokoroTTS(Settings(models_dir=tmp_path))
    with pytest.raises(ProviderConfigError):
        tts.voices()


def test_kokoro_builds_its_engine_once(monkeypatch, tmp_path) -> None:
    """M0 finding 2: engine construction is expensive; build it once, reuse it."""
    built = []

    class StubKokoro:
        def __init__(self, model_path, voices_path):
            built.append((model_path, voices_path))

        def get_voices(self):
            return ["af_heart"]

    monkeypatch.setattr(kokoro_onnx, "Kokoro", StubKokoro)
    (tmp_path / KOKORO_MODEL_FILE).write_bytes(b"stub")
    (tmp_path / KOKORO_VOICES_FILE).write_bytes(b"stub")
    tts = KokoroTTS(Settings(models_dir=tmp_path))
    assert tts.voices() == ["af_heart"]
    assert tts.voices() == ["af_heart"]
    assert len(built) == 1


def test_whisper_model_is_built_once_and_gets_the_hf_token(monkeypatch, tmp_path) -> None:
    """M0 findings 2 and 9: one WhisperModel, and an optional HF_TOKEN for the download."""
    built = []

    class StubWhisper:
        def __init__(self, size, **kwargs):
            built.append((size, kwargs))

        def transcribe(self, audio, **kwargs):
            return [], None

    monkeypatch.setattr(faster_whisper, "WhisperModel", StubWhisper)
    monkeypatch.setenv("HF_TOKEN", "hf_secret")
    stt = FasterWhisperSTT(Settings(models_dir=tmp_path))
    assert stt.transcribe_words(audio_path=tmp_path / "a.wav") == []
    assert stt.transcribe_words(audio_path=tmp_path / "b.wav") == []
    assert len(built) == 1
    assert built[0][1]["use_auth_token"] == "hf_secret"


def test_whisper_is_asked_for_word_timestamps_and_the_script_as_a_hint(monkeypatch, tmp_path):
    calls = []

    class StubWhisper:
        def __init__(self, size, **kwargs):
            pass

        def transcribe(self, audio, **kwargs):
            calls.append((audio, kwargs))
            return [], None

    monkeypatch.setattr(faster_whisper, "WhisperModel", StubWhisper)
    stt = FasterWhisperSTT(Settings(models_dir=tmp_path))
    stt.transcribe_words(audio_path=tmp_path / "a.wav", hint_text=SCRIPT)
    assert calls[0][1]["word_timestamps"] is True
    assert calls[0][1]["initial_prompt"] == SCRIPT


def test_kokoro_lists_real_voices(settings: Settings) -> None:
    voices = get_provider("tts", "kokoro", settings).voices()
    assert "af_heart" in voices


def test_kokoro_writes_24k_pcm16_and_reports_the_true_duration(narration) -> None:
    """M0 finding 4: soundfile silently downcasts float32, so the subtype is explicit."""
    info = soundfile.info(str(narration.path))
    assert info.samplerate == SAMPLE_RATE == narration.sample_rate
    assert info.channels == 1
    assert info.subtype == "PCM_16"
    assert narration.duration_s == pytest.approx(info.duration, abs=1e-3)
    assert narration.duration_s > 1.0


def test_transcription_snaps_back_to_the_script(settings: Settings, narration) -> None:
    stt = get_provider("stt", "fasterwhisper", settings)
    heard = stt.transcribe_words(audio_path=narration.path, hint_text=SCRIPT)
    assert heard, "whisper heard nothing in the narration"
    assert all(x.start_s <= x.end_s for x in heard)
    assert heard[-1].end_s <= narration.duration_s + 0.5

    snapped = snap_to_script(SCRIPT, heard)
    assert [x.word for x in snapped] == SCRIPT.split()
    for prev, nxt in pairwise(snapped):
        assert prev.end_s <= nxt.start_s + 1e-9
