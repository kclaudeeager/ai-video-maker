"""The reading end to end on the mock chain: real wav verses, a real FFmpeg concat,
a real mp3 with an audio stream — and the narration equal to the text."""

from videomaker.config import Settings
from videomaker.corpus.audio import build_reading, reader_deps
from videomaker.corpus.importer import work_dir
from videomaker.corpus.models import UnitRef
from videomaker.media.ffmpeg import probe_json
from videomaker.runner import PROVIDER_KINDS


def test_the_mock_chain_produces_a_playable_reading(tmp_path):
    settings = Settings(
        workspace_dir=tmp_path / "workspace",
        provider_chains={kind: ["mock"] for kind in PROVIDER_KINDS},
    )
    deps = reader_deps(settings, cache_dir=tmp_path / "cache")
    unit = deps.provider("corpus").unit(UnitRef(work_id="mock", book="JHN", chapter=3))

    reading = build_reading(unit, deps, voice="af_heart")

    root = work_dir(settings.workspace_dir, "mock")
    info = probe_json(root / reading.audio_relpath)
    assert info["format"]["format_name"] == "mp3"
    assert any(stream["codec_type"] == "audio" for stream in info["streams"])
    assert reading.duration_s > 0
    assert reading.provider == "mock"
    assert reading.language == "en"
    assert " ".join(seg.text for seg in reading.segments) == unit.plain
