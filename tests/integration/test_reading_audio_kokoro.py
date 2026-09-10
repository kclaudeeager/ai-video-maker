"""Reading audio through the **real** local voice, and the check that it makes a sound.

Everything else in the reader's audio tests runs on `MockTTS`, which writes
through `wave` and produces genuine 24 kHz PCM *silence*. That is the right mock —
it is real media and it costs nothing — but it is deaf to two whole classes of
failure, and both of them shipped:

* `MockTTS` ignores the name of the file it is given. `KokoroTTS` writes through
  `soundfile`, which infers the format from the extension and refuses one it does
  not know, so verse files named `.audio` (staged `.audio.part`) failed with
  `No format specified and unable to get format from file extension` while the
  offline suite stayed green.
* A silent take has the same byte count and the same duration as a spoken one.
  Checking that a file exists and is the right length proves nothing at all —
  which is why this asserts on **level**.

Needs the `ml` extra and the weights `videomaker setup` downloads, so it skips
cleanly without them, like `tests/unit/test_tts_stt_contracts.py`.
"""

import pytest

pytest.importorskip("kokoro_onnx", exc_type=ImportError)
pytest.importorskip("soundfile", exc_type=ImportError)

import soundfile

from videomaker.config import Settings
from videomaker.corpus.audio import build_reading, reader_deps
from videomaker.corpus.importer import work_dir
from videomaker.corpus.models import UnitRef, UnitText, Verse
from videomaker.providers.tts.kokoro_onnx import KOKORO_MODEL_FILE, KOKORO_VOICES_FILE
from videomaker.runner import PROVIDER_KINDS

pytestmark = pytest.mark.slow

#: Digital silence sits at the 16-bit noise floor. Real speech is orders of
#: magnitude above it; anything under this is not a voice.
MIN_RMS = 0.001


@pytest.fixture(scope="module")
def settings(tmp_path_factory) -> Settings:
    base = Settings()
    for name in (KOKORO_MODEL_FILE, KOKORO_VOICES_FILE):
        if not (base.models_dir / name).is_file():
            pytest.skip(f"{name} not downloaded; run `uv run videomaker setup`")
    return Settings(
        workspace_dir=tmp_path_factory.mktemp("kokoro-reader") / "workspace",
        # Every kind mocked except the voice: this is a test about the voice.
        provider_chains={**{kind: ["mock"] for kind in PROVIDER_KINDS}, "tts": ["kokoro"]},
    )


@pytest.fixture(scope="module")
def spoken(settings, tmp_path_factory):
    """One short passage, synthesised for real. Two verses keeps it under a minute."""
    deps = reader_deps(settings, cache_dir=tmp_path_factory.mktemp("kokoro-cache"))
    unit = UnitText(
        ref=UnitRef(work_id="mock", book="JHN", chapter=1),
        title="John 1",
        verses=[
            Verse(number=1, text="In the beginning was the Word."),
            Verse(number=2, text="The same was in the beginning with God."),
        ],
    )
    reading = build_reading(unit, deps, voice="af_heart")
    return reading, work_dir(settings.workspace_dir, "mock"), unit


def _rms(path) -> float:
    samples, _rate = soundfile.read(str(path))
    return float((samples.astype("float64") ** 2).mean() ** 0.5)


def test_the_real_voice_writes_every_verse_it_was_asked_for(spoken):
    """The naming bug: `soundfile` refuses a path whose extension it cannot map to
    a format, and the staging name has to keep that extension too."""
    reading, root, unit = spoken

    assert len(reading.segments) == len(unit.verses)
    for segment in reading.segments:
        assert (root / segment.audio_relpath).is_file()
    assert reading.provider == "kokoro"


def test_the_narration_is_audible(spoken):
    """A silent take is the same size and the same length as a spoken one, so the
    only honest check is the level."""
    reading, root, _unit = spoken

    for segment in reading.segments:
        rms = _rms(root / segment.audio_relpath)
        assert rms > MIN_RMS, f"verse {segment.verse} is silence ({rms:.6f} RMS)"


def test_the_stitched_mp3_is_audible_too(spoken):
    """The concat is where a working set of verses could still become silence."""
    import subprocess

    reading, root, _unit = spoken
    mp3 = root / reading.audio_relpath
    assert mp3.is_file()

    measured = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(mp3), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True,
        text=True,
        check=True,
    ).stderr
    mean = next(line for line in measured.splitlines() if "mean_volume" in line)
    decibels = float(mean.split("mean_volume:")[1].split("dB")[0])

    assert decibels > -60, f"the stitched narration is silence ({mean.strip()})"


def test_the_timings_come_from_the_real_audio(spoken):
    """Not from a word count: the durations are what the voice actually produced."""
    reading, root, _unit = spoken

    for segment in reading.segments:
        samples, rate = soundfile.read(str(root / segment.audio_relpath))
        assert len(samples) / rate == pytest.approx(segment.end_s - segment.start_s, abs=0.05)
