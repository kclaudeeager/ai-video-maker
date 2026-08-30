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
