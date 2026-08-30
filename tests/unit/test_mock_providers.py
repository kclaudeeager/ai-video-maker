import json
import subprocess
from pathlib import Path

import pytest

from videomaker.config import Settings
from videomaker.models import Aspect, AssetRef, VisualKind
from videomaker.providers import get_provider
from videomaker.providers.base import (
    ImageProvider,
    LLMProvider,
    LLMResult,
    StockProvider,
    STTProvider,
    TTSProvider,
    TTSResult,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MAX_FIXTURE_BYTES = 100 * 1024


def _probe(path: Path, entries: str) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", entries, "-of", "json", str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return json.loads(out)


def _duration_s(path: Path) -> float:
    return float(_probe(path, "format=duration")["format"]["duration"])


def _dimensions(path: Path) -> tuple[int, int]:
    stream = _probe(path, "stream=width,height")["streams"][0]
    return stream["width"], stream["height"]


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """A directory that looks like a project folder, so relative paths can be anchored."""
    (tmp_path / "project.json").write_text("{}")
    return tmp_path


def test_fixtures_exist_and_stay_under_100kb():
    for name in ("sample_photo.jpg", "sample_clip.mp4"):
        path = FIXTURES / name
        assert path.is_file(), f"missing fixture {path}"
        assert path.stat().st_size < MAX_FIXTURE_BYTES, f"{name} is {path.stat().st_size} bytes"


@pytest.mark.parametrize(
    ("kind", "abc"),
    [
        ("llm", LLMProvider),
        ("tts", TTSProvider),
        ("stt", STTProvider),
        ("image", ImageProvider),
        ("stock", StockProvider),
    ],
)
def test_every_mock_is_registered_and_implements_its_abc(kind, abc, settings):
    provider = get_provider(kind, "mock", settings)
    assert isinstance(provider, abc)


def test_llm_returns_deterministic_text(settings):
    llm = get_provider("llm", "mock", settings)
    first = llm.generate(system="sys", user="how ssds work")
    second = llm.generate(system="sys", user="how ssds work")
    assert isinstance(first, LLMResult)
    assert first.text.strip()
    assert first.model
    assert first.text == second.text
    assert llm.generate(system="sys", user="how cpus work").text != first.text


def test_llm_honours_a_json_schema(settings):
    llm = get_provider("llm", "mock", settings)
    schema = {
        "type": "object",
        "required": ["scenes"],
        "properties": {
            "scenes": {
                "type": "array",
                "minItems": 3,
                "items": {
                    "type": "object",
                    "required": ["narration", "visual_query"],
                    "properties": {
                        "narration": {"type": "string"},
                        "visual_query": {"type": "string"},
                    },
                },
            }
        },
    }
    result = llm.generate(system="sys", user="how ssds work", json_schema=schema)
    payload = json.loads(result.text)
    assert isinstance(payload["scenes"], list)
    assert len(payload["scenes"]) >= 3
    for scene in payload["scenes"]:
        assert scene["narration"].strip()
        assert scene["visual_query"].strip()


def test_tts_writes_a_real_wav_with_the_expected_duration(settings, tmp_path):
    tts = get_provider("tts", "mock", settings)
    text = "one two three four five six seven eight nine ten"  # 10 words -> 4.0 s
    out = tmp_path / "audio" / "s01.wav"
    result = tts.synthesize(text=text, voice="af_heart", out_path=out)

    assert isinstance(result, TTSResult)
    assert out.is_file()
    assert result.path == out
    assert result.sample_rate == 24000
    assert result.duration_s == pytest.approx(4.0, abs=0.05)
    assert _duration_s(out) == pytest.approx(4.0, abs=0.05)

    stream = _probe(out, "stream=sample_rate,channels,codec_name")["streams"][0]
    assert int(stream["sample_rate"]) == 24000
    assert int(stream["channels"]) == 1
    assert stream["codec_name"].startswith("pcm_")


def test_tts_is_deterministic(settings, tmp_path):
    tts = get_provider("tts", "mock", settings)
    a = tmp_path / "a.wav"
    b = tmp_path / "b.wav"
    tts.synthesize(text="deterministic silence please", voice="af_heart", out_path=a)
    tts.synthesize(text="deterministic silence please", voice="af_heart", out_path=b)
    assert a.read_bytes() == b.read_bytes()


def test_tts_voices_are_non_empty(settings):
    voices = get_provider("tts", "mock", settings).voices()
    assert voices and all(isinstance(v, str) for v in voices)


def test_tts_speed_shortens_the_clip(settings, tmp_path):
    tts = get_provider("tts", "mock", settings)
    out = tmp_path / "fast.wav"
    result = tts.synthesize(
        text="one two three four five six seven eight nine ten",
        voice="af_heart",
        out_path=out,
        speed=2.0,
    )
    assert result.duration_s == pytest.approx(2.0, abs=0.05)
    assert _duration_s(out) == pytest.approx(2.0, abs=0.05)


def test_stt_timings_are_monotonic_and_inside_the_audio(settings, tmp_path):
    tts = get_provider("tts", "mock", settings)
    stt = get_provider("stt", "mock", settings)
    text = "solid state drives store data in flash memory cells"
    out = tmp_path / "s01.wav"
    tts.synthesize(text=text, voice="af_heart", out_path=out)
    duration = _duration_s(out)

    words = stt.transcribe_words(audio_path=out, hint_text=text)
    assert [w.word for w in words] == text.split()

    previous_end = 0.0
    for word in words:
        assert word.start_s >= previous_end - 1e-6
        assert word.end_s >= word.start_s
        previous_end = word.end_s
    assert words[0].start_s >= 0.0
    assert words[-1].end_s <= duration + 1e-6
    assert words[-1].end_s > duration * 0.5  # timings cover the clip, not just its head


def test_stt_without_a_hint_still_covers_the_clip(settings, tmp_path):
    tts = get_provider("tts", "mock", settings)
    stt = get_provider("stt", "mock", settings)
    out = tmp_path / "s01.wav"
    tts.synthesize(text="four words go here", voice="af_heart", out_path=out)
    words = stt.transcribe_words(audio_path=out)
    assert words
    assert words[-1].end_s <= _duration_s(out) + 1e-6


@pytest.mark.parametrize("per_page", [1, 4, 7])
def test_stock_search_returns_exactly_per_page_results(settings, per_page):
    stock = get_provider("stock", "mock", settings)
    results = stock.search(query="ssd macro", kind=VisualKind.STOCK_VIDEO, per_page=per_page)
    assert len(results) == per_page
    assert len({r.source_id for r in results}) == per_page
    assert all(r.provider == "mock" for r in results)
    assert all(r.duration_s for r in results)


def test_stock_photo_search_results_have_no_duration(settings):
    stock = get_provider("stock", "mock", settings)
    results = stock.search(query="ssd macro", kind=VisualKind.STOCK_PHOTO, per_page=2)
    assert len(results) == 2
    assert all(r.duration_s is None for r in results)


def test_stock_download_copies_the_video_fixture(settings, project_dir):
    stock = get_provider("stock", "mock", settings)
    result = stock.search(query="ssd macro", kind=VisualKind.STOCK_VIDEO, per_page=1)[0]
    out = project_dir / "scenes" / "s01" / "clip.mp4"
    ref = stock.download(result, out)

    assert isinstance(ref, AssetRef)
    assert out.read_bytes() == (FIXTURES / "sample_clip.mp4").read_bytes()
    assert ref.local_path == "scenes/s01/clip.mp4"
    assert (ref.width, ref.height) == _dimensions(out)
    assert ref.duration_s == pytest.approx(_duration_s(out), abs=0.05)
    assert ref.provider == "mock"
    assert ref.source_id == result.source_id


def test_stock_download_of_a_photo_copies_the_photo_fixture(settings, project_dir):
    stock = get_provider("stock", "mock", settings)
    result = stock.search(query="ssd macro", kind=VisualKind.STOCK_PHOTO, per_page=1)[0]
    out = project_dir / "scenes" / "s01" / "photo.jpg"
    ref = stock.download(result, out)
    assert out.read_bytes() == (FIXTURES / "sample_photo.jpg").read_bytes()
    assert ref.duration_s is None
    assert (ref.width, ref.height) == _dimensions(out)


@pytest.mark.parametrize(
    ("aspect", "expected"),
    [(Aspect.WIDE, (1920, 1080)), (Aspect.VERTICAL, (1080, 1920))],
)
def test_image_generate_matches_the_requested_aspect(settings, project_dir, aspect, expected):
    image = get_provider("image", "mock", settings)
    out = project_dir / "scenes" / "s01" / "ai.jpg"
    ref = image.generate_image(prompt="a cutaway of an ssd", out_path=out, aspect=aspect)

    assert isinstance(ref, AssetRef)
    assert out.is_file()
    assert _dimensions(out) == expected
    assert (ref.width, ref.height) == expected
    assert ref.local_path == "scenes/s01/ai.jpg"
    assert ref.provider == "mock"
    assert ref.duration_s is None
