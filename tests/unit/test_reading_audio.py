"""Reading audio built straight from the text.

`test_narration_is_the_source_text` is the most important test in the plan: the
narration is the source segmented by rule, and this is the assertion that the rule
dropped nothing and added nothing. Mutation-tested by making
`segment_for_reading` drop a word.

The other guarantees: segments tile the audio with no gap and no overlap, the VTT
has one cue per verse, an identical second build makes zero provider calls, and a
run interrupted by a vendor resumes from the first missing verse rather than
starting over.
"""

import re
import wave
from pathlib import Path

import httpx
import pytest

from videomaker.config import Settings
from videomaker.corpus import audio as audio_module
from videomaker.corpus.audio import (
    VERSE_SUFFIX,
    Reading,
    build_reading,
    reader_deps,
    reading_key,
    segment_for_reading,
)
from videomaker.corpus.importer import work_dir
from videomaker.corpus.models import UnitRef, UnitText, Verse
from videomaker.providers.mock import MockTTS
from videomaker.providers.ratelimit import QuotaTracker
from videomaker.providers.tts.http_api import (
    HTTPTTSConfig,
    HTTPTTSProvider,
    RateLimited,
    TTSBudgetExceeded,
)
from videomaker.runner import PROVIDER_KINDS

VOICE = "af_heart"
#: The mp3 container is longer than the verses it holds by LAME's encoder delay
#: (1,105 samples) plus up to one frame (1,152 samples) of end padding — 46-94 ms
#: at 24 kHz, measured 2026-09-10. The plan's 50 ms was written before that was
#: measured; browsers trim the delay on playback, so the segments stay right.
MP3_PADDING_S = 0.1


def normalise_ws(text: str) -> str:
    return " ".join(text.split())


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        workspace_dir=tmp_path / "workspace",
        provider_chains={kind: ["mock"] for kind in PROVIDER_KINDS},
    )


@pytest.fixture
def mock_deps(settings, tmp_path):
    return reader_deps(settings, cache_dir=tmp_path / "cache")


@pytest.fixture
def sample_unit(mock_deps):
    return mock_deps.provider("corpus").unit(UnitRef(work_id="mock", book="JHN", chapter=2))


def counting(deps, kind: str = "tts") -> list[str]:
    """Wrap the chain's provider so every synthesis is recorded."""
    calls: list[str] = []
    provider = deps.provider(kind)
    real = provider.synthesize

    def spy(**kwargs):
        calls.append(kwargs["text"])
        return real(**kwargs)

    provider.synthesize = spy
    return calls


# --------------------------------------------------------------------- fidelity


def test_narration_is_the_source_text(mock_deps, sample_unit):
    reading = build_reading(sample_unit, mock_deps, voice="mock", confirmed=True)
    spoken = " ".join(seg.text for seg in reading.segments)
    assert normalise_ws(spoken) == normalise_ws(sample_unit.plain)


def test_segments_are_the_verses_and_nothing_else(sample_unit):
    assert segment_for_reading(sample_unit) == [(v.number, v.text) for v in sample_unit.verses]


# ---------------------------------------------------------------------- timing


def test_segments_tile_the_audio(mock_deps, sample_unit):
    reading = build_reading(sample_unit, mock_deps, voice=VOICE)
    assert reading.segments[0].start_s == 0.0
    for earlier, later in zip(reading.segments, reading.segments[1:], strict=False):
        assert later.start_s == pytest.approx(earlier.end_s)
        assert later.end_s > later.start_s
    assert reading.segments[-1].end_s == pytest.approx(reading.duration_s, abs=MP3_PADDING_S)
    assert reading.duration_s >= reading.segments[-1].end_s


def test_the_vtt_has_one_cue_per_verse(mock_deps, sample_unit, settings):
    reading = build_reading(sample_unit, mock_deps, voice=VOICE)
    vtt = (work_dir(settings.workspace_dir, "mock") / reading.vtt_relpath).read_text()
    assert vtt.startswith("WEBVTT")
    cues = re.findall(r"^\d\d:\d\d:\d\d\.\d{3} --> \d\d:\d\d:\d\d\.\d{3}$", vtt, re.MULTILINE)
    assert len(cues) == len(sample_unit.verses)
    # The payload is the verse number, which is what the player lights.
    assert re.search(r"--> [\d:.]+\n2\n", vtt)


def test_each_verse_is_its_own_file_and_they_add_up(mock_deps, sample_unit, settings):
    reading = build_reading(sample_unit, mock_deps, voice=VOICE)
    root = work_dir(settings.workspace_dir, "mock")
    total = 0.0
    for segment in reading.segments:
        with wave.open(str(root / segment.audio_relpath), "rb") as handle:
            total += handle.getnframes() / handle.getframerate()
    assert total == pytest.approx(reading.segments[-1].end_s, abs=0.001)


# --------------------------------------------------------------------- caching


def test_a_second_build_makes_zero_provider_calls(mock_deps, sample_unit, monkeypatch):
    calls = counting(mock_deps)
    first = build_reading(sample_unit, mock_deps, voice=VOICE)
    assert len(calls) == len(sample_unit.verses)

    # ...and does no work of its own either. Verse files alone would make the
    # provider count zero while still re-encoding the mp3 every time the page is
    # opened, so the finished `reading.json` is what has to be the answer.
    encodes: list[list[str]] = []
    monkeypatch.setattr(
        audio_module, "run_ffmpeg", lambda args, **kw: encodes.append(args) or ""
    )
    second = build_reading(sample_unit, mock_deps, voice=VOICE)
    assert len(calls) == len(sample_unit.verses)
    assert encodes == []
    assert second == first


def test_changing_the_voice_changes_the_key_and_keeps_the_old_reading(
    mock_deps, sample_unit, settings
):
    a = reading_key(sample_unit, provider="mock", voice="af_heart", speed=1.0)
    b = reading_key(sample_unit, provider="mock", voice="am_adam", speed=1.0)
    c = reading_key(sample_unit, provider="mock", voice="af_heart", speed=1.2)
    assert len({a, b, c}) == 3
    first = build_reading(sample_unit, mock_deps, voice="af_heart")
    second = build_reading(sample_unit, mock_deps, voice="am_adam")
    root = work_dir(settings.workspace_dir, "mock")
    assert (root / first.audio_relpath).is_file()
    assert (root / second.audio_relpath).is_file()
    assert first.audio_relpath != second.audio_relpath


def test_an_interrupted_run_resumes_from_the_first_missing_verse(
    mock_deps, sample_unit, settings
):
    calls = counting(mock_deps)
    reading = build_reading(sample_unit, mock_deps, voice=VOICE)
    root = work_dir(settings.workspace_dir, "mock")
    (root / Path(reading.audio_relpath).parent / audio_module.READING_FILE).unlink()
    (root / reading.segments[1].audio_relpath).unlink()
    calls.clear()
    again = build_reading(sample_unit, mock_deps, voice=VOICE)
    assert calls == [sample_unit.verses[1].text]
    assert again.segments == reading.segments


def test_a_verse_whose_synthesis_died_mid_write_is_not_kept(mock_deps, sample_unit, settings):
    """A provider that writes some bytes and then fails must leave nothing behind.

    A half-written file that looks present is worse than an absent one: the next
    run would stitch it in and the reader would hear a click where a verse was.
    """
    provider = mock_deps.provider("tts")
    real = provider.synthesize
    failed: list[Path] = []

    def die_on_the_second(**kwargs):
        if kwargs["text"] == sample_unit.verses[1].text:
            Path(kwargs["out_path"]).parent.mkdir(parents=True, exist_ok=True)
            Path(kwargs["out_path"]).write_bytes(b"RIFF truncated")
            failed.append(Path(kwargs["out_path"]))
            raise RateLimited("the vendor stopped mid-verse")
        return real(**kwargs)

    provider.synthesize = die_on_the_second
    with pytest.raises(RateLimited):
        build_reading(sample_unit, mock_deps, voice=VOICE)

    key = reading_key(sample_unit, provider="mock", voice=VOICE, speed=1.0)
    verses = (
        work_dir(settings.workspace_dir, "mock")
        / "derived"
        / audio_module.AUDIO_DIRNAME
        / key
        / audio_module.VERSES_DIRNAME
    )
    assert failed and not (verses / f"002{audio_module.VERSE_SUFFIX}").is_file()
    assert (verses / f"001{audio_module.VERSE_SUFFIX}").is_file()

    provider.synthesize = real
    reading = build_reading(sample_unit, mock_deps, voice=VOICE)
    assert len(reading.segments) == len(sample_unit.verses)
    assert all(seg.end_s > seg.start_s for seg in reading.segments)


def test_a_half_written_verse_is_not_mistaken_for_a_verse(mock_deps, sample_unit, settings):
    key = reading_key(sample_unit, provider="mock", voice=VOICE, speed=1.0)
    verses = (
        work_dir(settings.workspace_dir, "mock")
        / "derived"
        / audio_module.AUDIO_DIRNAME
        / key
        / audio_module.VERSES_DIRNAME
    )
    verses.mkdir(parents=True)
    (verses / f"002{audio_module.VERSE_SUFFIX}.part").write_bytes(b"RIFF....")
    calls = counting(mock_deps)
    build_reading(sample_unit, mock_deps, voice=VOICE)
    assert len(calls) == len(sample_unit.verses)


# ------------------------------------------------------------------ a paid voice


def wav_bytes(seconds: float, tmp: Path) -> bytes:
    with wave.open(str(tmp), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(bytes(int(8000 * seconds) * 2))
    return tmp.read_bytes()


@pytest.fixture
def paid_deps(tmp_path, monkeypatch):
    """A vendor behind `MockTransport` that fails with 429 after `fail_after` calls."""
    monkeypatch.setenv("VOICE_KEY", "k")
    monkeypatch.setattr("videomaker.providers.tts.http_api.sleep", lambda s: None)
    cfg = HTTPTTSConfig(
        name="vendor",
        endpoint="https://voice.invalid/speak",
        api_key_env="VOICE_KEY",
        languages=["en"],
        voices=["nyira"],
        cost_per_minute_usd=0.10,
        retry_attempts=1,
    )
    settings = Settings(
        workspace_dir=tmp_path / "workspace",
        provider_chains={**{kind: ["mock"] for kind in PROVIDER_KINDS}, "tts": ["vendor"]},
        voice_providers=[cfg.model_dump()],
    )
    deps = reader_deps(settings, cache_dir=tmp_path / "cache")
    calls: list[httpx.Request] = []
    state = {"fail_after": None}
    audio = wav_bytes(0.25, tmp_path / "probe.wav")

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if state["fail_after"] is not None and len(calls) > state["fail_after"]:
            return httpx.Response(429, content=b"busy")
        return httpx.Response(200, content=audio)

    provider = HTTPTTSProvider(cfg)
    provider.client = httpx.Client(transport=httpx.MockTransport(handler))
    provider.quota = QuotaTracker(tmp_path / "quota.json")
    deps.instances[("tts", "vendor")] = provider
    deps.calls = calls
    deps.state = state
    return deps


def test_a_paid_run_over_budget_is_refused_before_any_request(paid_deps):
    # 4,600 words is just over the default 30 minutes at 150 wpm — and $3.07 at
    # $0.10/min, over the $1.00 default too. Either limit alone would refuse it.
    long_unit = UnitText(
        ref=UnitRef(work_id="mock", book="JHN", chapter=1),
        title="John 1",
        verses=[Verse(number=1, text=" ".join(["word"] * 4600))],
    )
    with pytest.raises(TTSBudgetExceeded, match="--yes"):
        build_reading(long_unit, paid_deps, voice="nyira")
    assert paid_deps.calls == []
    reading = build_reading(long_unit, paid_deps, voice="nyira", confirmed=True)
    assert len(paid_deps.calls) == 1
    assert isinstance(reading, Reading)


def test_the_whole_run_is_priced_before_the_first_request(paid_deps):
    """The refusal has to name the run's bill, not the first verse's share of it.

    Verse by verse each of these is cheap and the provider's own guard would let
    several through before tripping — spending real money on a run that was always
    going to be refused.
    """
    verses = [Verse(number=n, text=" ".join(["word"] * 400)) for n in range(1, 6)]
    unit = UnitText(
        ref=UnitRef(work_id="mock", book="JHN", chapter=1), title="John 1", verses=verses
    )
    with pytest.raises(TTSBudgetExceeded):
        build_reading(unit, paid_deps, voice="nyira")
    assert paid_deps.calls == []


def test_a_run_that_would_cross_the_daily_cap_stops_before_the_first_verse(paid_deps):
    """The daily cap is checked against the *whole* run, not one verse at a time.

    A per-call check would start a chapter with two requests left and fail at verse
    three, having spent them for nothing.
    """
    vendor = paid_deps.provider("tts")
    vendor.cfg = vendor.cfg.model_copy(update={"requests_per_day": 2})
    unit = paid_deps.provider("corpus").unit(UnitRef(work_id="mock", book="JHN", chapter=1))
    assert len(unit.verses) > 2
    with pytest.raises(RateLimited, match="resets in"):
        build_reading(unit, paid_deps, voice="nyira", confirmed=True)
    assert paid_deps.calls == []


def test_a_rate_limited_run_keeps_its_verses_and_resumes(paid_deps):
    unit = paid_deps.provider("corpus").unit(UnitRef(work_id="mock", book="JHN", chapter=1))
    paid_deps.state["fail_after"] = 2
    with pytest.raises(RateLimited):
        build_reading(unit, paid_deps, voice="nyira", confirmed=True)
    made = len(paid_deps.calls)
    paid_deps.state["fail_after"] = None
    paid_deps.calls.clear()
    reading = build_reading(unit, paid_deps, voice="nyira", confirmed=True)
    # Two verses were already on disk; only the third was synthesised.
    assert made == 3
    assert len(paid_deps.calls) == len(unit.verses) - 2
    assert len(reading.segments) == len(unit.verses)


# ------------------------------- the name of the file the provider is handed
#
# `MockTTS` writes through `wave`, which does not care what the file is called.
# Every real provider does: `KokoroTTS` writes through `soundfile`, and
# libsndfile infers the output format from the extension. The verse files were
# `.audio`, staged as `.audio.part`, so real synthesis died on
#   No format specified and unable to get format from file extension
# while the whole offline suite stayed green. These two tests are that gap.


class ExtensionSensitiveTTS(MockTTS):
    """A mock that refuses a name libsndfile would refuse — which is all of them
    except a real audio extension. Faithful stand-in for `soundfile.write`."""

    KNOWN = frozenset({".wav", ".flac", ".ogg", ".mp3", ".aiff", ".au"})

    def synthesize(self, **kwargs):
        out = Path(kwargs["out_path"])
        if out.suffix.lower() not in self.KNOWN:
            raise RuntimeError(
                f"No format specified and unable to get format from file extension: {out}"
            )
        return super().synthesize(**kwargs)


def test_every_path_a_provider_is_handed_is_one_it_can_write(mock_deps, sample_unit):
    """Including the staging name, which is where this actually broke."""
    mock_deps.instances[("tts", "mock")] = ExtensionSensitiveTTS(mock_deps.settings)

    reading = build_reading(sample_unit, mock_deps, voice=VOICE)

    assert len(reading.segments) == len(sample_unit.verses)
    assert all(segment.end_s > segment.start_s for segment in reading.segments)


def test_the_staging_name_keeps_the_extension(mock_deps, sample_unit, settings):
    """`001.part.wav`, not `001.wav.part`. The writer sees the suffix either way,
    and only one of them is a format it knows."""
    seen: list[str] = []
    provider = mock_deps.provider("tts")
    real = provider.synthesize

    def watch(**kwargs):
        seen.append(Path(kwargs["out_path"]).name)
        return real(**kwargs)

    provider.synthesize = watch
    build_reading(sample_unit, mock_deps, voice=VOICE)

    assert seen, "the provider was asked for something"
    for name in seen:
        assert name.endswith(VERSE_SUFFIX), f"{name} is not a name a writer would accept"
        assert ".part" in name, "and it is still staged rather than written in place"
