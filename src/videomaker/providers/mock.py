"""Offline mock implementations of every provider ABC.

Registered under the name ``mock`` for each kind, so ``--providers mock`` runs the
whole pipeline with no network, no quota and no model weights — while still
producing **real** media: a genuine 24 kHz PCM wav for narration and the checked-in
FFmpeg-generated fixtures for visuals. The golden-path test drives real FFmpeg over
these artefacts, so anything that fakes a file rather than writing one would make
that test prove nothing.

Deliberately stdlib-only (``wave``, ``shutil``, ``hashlib``): the mocks must work in
CI, which installs without the optional ``ml`` extra (no ``soundfile``).
"""

import hashlib
import json
import shutil
import subprocess
import wave
from pathlib import Path

from videomaker.config import Settings
from videomaker.models import Aspect, AssetRef, StockResult, VisualKind, WordTiming
from videomaker.project import PROJECT_FILE
from videomaker.providers import register
from videomaker.providers.base import (
    ImageProvider,
    LLMProvider,
    LLMResult,
    StockProvider,
    STTProvider,
    TTSProvider,
    TTSResult,
)
from videomaker.providers.errors import ProviderError

PROVIDER_NAME = "mock"
MOCK_MODEL = "mock-llm-v1"

FIXTURES_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures"
PHOTO_FIXTURE = FIXTURES_DIR / "sample_photo.jpg"
CLIP_FIXTURE = FIXTURES_DIR / "sample_clip.mp4"
# Properties of the committed fixtures; asserted against ffprobe in the unit tests.
FIXTURE_WIDTH = 1920
FIXTURE_HEIGHT = 1080
CLIP_DURATION_S = 8.0

SAMPLE_RATE = 24000  # matches Kokoro (M0 finding 4), so mixes need no resampling
SAMPLE_WIDTH_BYTES = 2  # 16-bit signed PCM
SECONDS_PER_WORD = 0.4
MIN_DURATION_S = SECONDS_PER_WORD  # never write a zero-length wav: FFmpeg rejects it

ASPECT_DIMENSIONS: dict[Aspect, tuple[int, int]] = {
    Aspect.WIDE: (1920, 1080),
    Aspect.VERTICAL: (1080, 1920),
}

MOCK_VOICES = ["af_heart", "af_sky", "am_adam"]
_NARRATION_FIELDS = frozenset({"narration", "script", "text", "body", "voiceover"})
_DEFAULT_ARRAY_ITEMS = 3


def _fixture(path: Path) -> Path:
    """Fixtures live in the repo, not the wheel; fail loudly rather than half-working."""
    if not path.is_file():
        raise ProviderError(f"missing test fixture {path}; the mock providers need the repo tree")
    return path


def _digest(*parts: object) -> str:
    """Short stable hex digest — the mocks are deterministic, never random."""
    joined = "\x1f".join(str(p) for p in parts)
    return hashlib.sha256(joined.encode()).hexdigest()[:12]


def _project_relative(out_path: Path) -> str:
    """`AssetRef.local_path` is always relative to the project folder.

    The project folder is the nearest ancestor holding a `project.json`. Outside a
    project (unit tests writing into a bare tmp dir) fall back to the bare filename,
    which is still a valid relative path.
    """
    path = Path(out_path).resolve()
    for parent in path.parents:
        if (parent / PROJECT_FILE).is_file():
            return path.relative_to(parent).as_posix()
    return path.name


def _audio_duration_s(audio_path: Path) -> float:
    """Real duration of `audio_path`: read wav headers directly, else ask ffprobe."""
    path = Path(audio_path)
    if path.suffix.lower() == ".wav":
        with wave.open(str(path), "rb") as wav:
            return wav.getnframes() / float(wav.getframerate())
    out = _run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)]
    )
    return float(out.strip())


def _run(args: list[str]) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True, check=True)
    except FileNotFoundError as exc:
        raise ProviderError(f"{args[0]} is not on PATH; the mock providers need it") from exc
    except subprocess.CalledProcessError as exc:
        raise ProviderError(f"{args[0]} failed: {exc.stderr.strip()}") from exc
    return result.stdout


# --------------------------------------------------------------------------- LLM


def _mock_string(field: str, topic: str) -> str:
    if field in _NARRATION_FIELDS:
        return (
            f"Mock narration about {topic}, written with enough real words "
            "that the offline pipeline has something to speak and align."
        )
    if field:
        return f"mock {field.replace('_', ' ')} for {topic}"
    return f"mock text for {topic}"


def _instance_from_schema(schema: dict, topic: str, field: str = "") -> object:
    """A deterministic instance satisfying `schema`, so the script stage can validate it."""
    if schema.get("enum"):
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        properties: dict = schema.get("properties") or {}
        names = schema.get("required") or list(properties)
        return {
            name: _instance_from_schema(properties.get(name) or {}, topic, name) for name in names
        }
    if kind == "array":
        count = max(int(schema.get("minItems", _DEFAULT_ARRAY_ITEMS)), 1)
        items: dict = schema.get("items") or {}
        return [
            _instance_from_schema(items, f"{topic} (part {index + 1})", field)
            for index in range(count)
        ]
    if kind == "integer":
        return int(schema.get("minimum", 1))
    if kind == "number":
        return float(schema.get("minimum", 1.0))
    if kind == "boolean":
        return True
    if kind == "null":
        return None
    return _mock_string(field, topic)


@register("llm", PROVIDER_NAME)
class MockLLM(LLMProvider):
    """Deterministic text generation. With a `json_schema`, emits a conforming instance."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def generate(
        self,
        *,
        system: str,
        user: str,
        json_schema: dict | None = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> LLMResult:
        topic = user.strip() or "an unnamed topic"
        if json_schema:
            text = json.dumps(_instance_from_schema(json_schema, topic), indent=2)
        else:
            text = (
                f"Mock script for {topic}.\n\n"
                "This text is generated offline and never touches a provider.\n"
                f"Request fingerprint: {_digest(system, user, temperature, max_tokens)}."
            )
        return LLMResult(text=text, model=MOCK_MODEL, cached=False)


# --------------------------------------------------------------------------- TTS


@register("tts", PROVIDER_NAME)
class MockTTS(TTSProvider):
    """Writes real 24 kHz mono 16-bit PCM silence, 0.4 s per word (divided by `speed`)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def voices(self) -> list[str]:
        return list(MOCK_VOICES)

    def synthesize(
        self,
        *,
        text: str,
        voice: str,
        out_path: Path,
        speed: float = 1.0,
        language: str = "en",
    ) -> TTSResult:
        word_count = len(text.split())
        duration_s = max(word_count * SECONDS_PER_WORD / max(speed, 0.01), MIN_DURATION_S)
        path = Path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        frames = round(duration_s * SAMPLE_RATE)
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(SAMPLE_WIDTH_BYTES)
            wav.setframerate(SAMPLE_RATE)
            wav.writeframes(bytes(frames * SAMPLE_WIDTH_BYTES))
        return TTSResult(path=path, duration_s=frames / SAMPLE_RATE, sample_rate=SAMPLE_RATE)


# --------------------------------------------------------------------------- STT


@register("stt", PROVIDER_NAME)
class MockSTT(STTProvider):
    """Evenly spaced word timings covering the whole clip, derived from `hint_text`."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def transcribe_words(
        self,
        *,
        audio_path: Path,
        language: str = "en",
        hint_text: str = "",
    ) -> list[WordTiming]:
        duration_s = _audio_duration_s(audio_path)
        words = hint_text.split()
        if not words:
            words = ["mock"] * max(round(duration_s / SECONDS_PER_WORD), 1)
        step = duration_s / len(words)
        timings: list[WordTiming] = []
        for index, word in enumerate(words):
            # Zero-gap boundaries, exactly as faster-whisper emits them (M0 finding 3).
            start_s = round(index * step, 3)
            end_s = min(round((index + 1) * step, 3), duration_s)
            timings.append(WordTiming(word=word, start_s=start_s, end_s=max(end_s, start_s)))
        return timings


# ------------------------------------------------------------------------- stock


def _is_video(kind: VisualKind) -> bool:
    return kind is not VisualKind.STOCK_PHOTO and kind is not VisualKind.AI_IMAGE


@register("stock", PROVIDER_NAME)
class MockStock(StockProvider):
    """Search returns exactly `per_page` synthetic hits; download copies a fixture."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def search(
        self,
        *,
        query: str,
        kind: VisualKind,
        min_duration_s: float = 0.0,
        orientation: str = "landscape",
        per_page: int = 4,
    ) -> list[StockResult]:
        video = _is_video(kind)
        fixture = _fixture(CLIP_FIXTURE if video else PHOTO_FIXTURE)
        results: list[StockResult] = []
        for index in range(max(per_page, 0)):
            source_id = f"mock-{_digest(query, kind, orientation, index)}"
            results.append(
                StockResult(
                    provider=PROVIDER_NAME,
                    source_id=source_id,
                    source_url=f"https://mock.invalid/{'video' if video else 'photo'}/{source_id}",
                    preview_url=f"https://mock.invalid/preview/{source_id}.jpg",
                    download_url=fixture.as_uri(),
                    width=FIXTURE_WIDTH,
                    height=FIXTURE_HEIGHT,
                    # The only clip on hand is 8 s; a longer `min_duration_s` is reported
                    # honestly rather than faked, so callers exercise their own trimming.
                    duration_s=CLIP_DURATION_S if video else None,
                    attribution="Mock Fixtures",
                    license="CC0 (synthetic test fixture)",
                )
            )
        return results

    def download(
        self,
        result: StockResult,
        out_path: Path,
        *,
        max_height: int = 1080,
    ) -> AssetRef:
        # `max_height` is ignored: both fixtures are already 1080-tall.
        video = result.download_url.endswith(".mp4") or result.duration_s is not None
        fixture = _fixture(CLIP_FIXTURE if video else PHOTO_FIXTURE)
        path = Path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(fixture, path)
        return AssetRef(
            provider=PROVIDER_NAME,
            source_id=result.source_id,
            source_url=result.source_url,
            local_path=_project_relative(path),
            width=FIXTURE_WIDTH,
            height=FIXTURE_HEIGHT,
            duration_s=CLIP_DURATION_S if video else None,
            attribution=result.attribution,
            license=result.license,
        )


# ------------------------------------------------------------------------- image


@register("image", PROVIDER_NAME)
class MockImage(ImageProvider):
    """Copies the photo fixture, resizing only when the aspect is not the fixture's 16:9."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def generate_image(
        self,
        *,
        prompt: str,
        out_path: Path,
        aspect: Aspect,
        negative_prompt: str = "",
        seed: int | None = None,
    ) -> AssetRef:
        width, height = ASPECT_DIMENSIONS.get(aspect, (FIXTURE_WIDTH, FIXTURE_HEIGHT))
        path = Path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if (width, height) == (FIXTURE_WIDTH, FIXTURE_HEIGHT):
            shutil.copyfile(_fixture(PHOTO_FIXTURE), path)
        else:
            _run(
                [
                    "ffmpeg",
                    "-y",
                    "-v",
                    "error",
                    "-i",
                    str(_fixture(PHOTO_FIXTURE)),
                    "-vf",
                    (
                        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                        f"crop={width}:{height}"
                    ),
                    "-frames:v",
                    "1",
                    str(path),
                ]
            )
        source_id = f"mock-{_digest(prompt, aspect, negative_prompt, seed)}"
        return AssetRef(
            provider=PROVIDER_NAME,
            source_id=source_id,
            source_url=f"https://mock.invalid/image/{source_id}",
            local_path=_project_relative(path),
            width=width,
            height=height,
            duration_s=None,
            attribution="Mock Fixtures",
            license="CC0 (synthetic test fixture)",
        )
