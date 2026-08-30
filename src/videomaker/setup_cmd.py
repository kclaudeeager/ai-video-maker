import time
from pathlib import Path

from videomaker.config import Settings
from videomaker.downloads import download_file

_RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
KOKORO_MODEL_URL = f"{_RELEASE}/kokoro-v1.0.onnx"
KOKORO_VOICES_URL = f"{_RELEASE}/voices-v1.0.bin"

MODEL_TARGETS = {
    KOKORO_MODEL_URL: "kokoro-v1.0.onnx",   # ~310 MB
    KOKORO_VOICES_URL: "voices-v1.0.bin",   # ~27 MB
}


def ensure_models(settings: Settings, downloader=download_file) -> list[Path]:
    paths = []
    for url, filename in MODEL_TARGETS.items():
        dest = settings.models_dir / filename
        if not dest.exists():
            downloader(url, dest)
        paths.append(dest)
    return paths


def spike_tts(settings: Settings) -> tuple[Path, float, float]:
    """Synthesize a short line with Kokoro; returns (wav_path, audio_s, wall_s)."""
    import soundfile as sf
    from kokoro_onnx import Kokoro

    kokoro = Kokoro(
        str(settings.models_dir / "kokoro-v1.0.onnx"),
        str(settings.models_dir / "voices-v1.0.bin"),
    )
    started = time.monotonic()
    samples, sample_rate = kokoro.create(
        "This is a Kokoro voice test on this machine.", voice="af_heart", speed=1.0
    )
    wall_s = time.monotonic() - started
    out = settings.models_dir / "smoke_test.wav"
    sf.write(str(out), samples, sample_rate)
    audio_s = len(samples) / sample_rate
    return out, audio_s, wall_s


def spike_stt(settings: Settings, wav: Path) -> list[tuple[str, float, float]]:
    """Transcribe the smoke wav with word timestamps; returns (word, start, end) tuples."""
    from faster_whisper import WhisperModel

    model = WhisperModel(
        "base", device="cpu", compute_type="int8",
        download_root=str(settings.models_dir / "whisper"),
    )
    segments, _info = model.transcribe(str(wav), word_timestamps=True)
    words: list[tuple[str, float, float]] = []
    for segment in segments:
        for word in segment.words or []:
            words.append((word.word.strip(), word.start, word.end))
    return words
