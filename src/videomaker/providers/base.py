from abc import ABC, abstractmethod
from pathlib import Path

from pydantic import BaseModel

from videomaker.models import Aspect, AssetRef, StockResult, VisualKind, WordTiming


class LLMResult(BaseModel):
    text: str
    model: str
    cached: bool = False


class TTSResult(BaseModel):
    path: Path
    duration_s: float
    sample_rate: int


class LLMProvider(ABC):
    """Text generation. Synchronous: the pipeline runs in a worker thread."""

    @abstractmethod
    def generate(
        self,
        *,
        system: str,
        user: str,
        json_schema: dict | None = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> LLMResult: ...


class TTSProvider(ABC):
    """Narration synthesis."""

    @abstractmethod
    def voices(self) -> list[str]: ...

    @abstractmethod
    def synthesize(
        self,
        *,
        text: str,
        voice: str,
        out_path: Path,
        speed: float = 1.0,
        language: str = "en",
    ) -> TTSResult: ...


class STTProvider(ABC):
    """Forced alignment: narration audio to word-level timings."""

    @abstractmethod
    def transcribe_words(
        self,
        *,
        audio_path: Path,
        language: str = "en",
        hint_text: str = "",
    ) -> list[WordTiming]: ...


class ImageProvider(ABC):
    """Text-to-image generation."""

    @abstractmethod
    def generate_image(
        self,
        *,
        prompt: str,
        out_path: Path,
        aspect: Aspect,
        negative_prompt: str = "",
        seed: int | None = None,
    ) -> AssetRef: ...


class StockProvider(ABC):
    """Stock footage and photo search plus download."""

    @abstractmethod
    def search(
        self,
        *,
        query: str,
        kind: VisualKind,
        min_duration_s: float = 0.0,
        orientation: str = "landscape",
        per_page: int = 4,
    ) -> list[StockResult]: ...

    @abstractmethod
    def download(
        self,
        result: StockResult,
        out_path: Path,
        *,
        max_height: int = 1080,
    ) -> AssetRef: ...


class Uploader(ABC):
    """Publishing target. Declared in M1, implemented in M5.

    PROVISIONAL SIGNATURE — nothing in M1 calls this. Spec 4.6 specifies
    ``upload(*, video_path, metadata: VideoMetadata, privacy="private",
    thumbnail_path, progress) -> UploadResult``; those two types do not exist
    until M5, so this stands in for them. M5 owns settling it.
    """

    @abstractmethod
    def upload(
        self,
        *,
        video_path: Path,
        title: str,
        description: str = "",
        tags: list[str] | None = None,
        privacy: str = "private",
    ) -> str: ...
