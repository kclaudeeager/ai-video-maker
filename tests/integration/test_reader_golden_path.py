"""The reader, end to end in the browser, on the mock chain.

Library -> work -> chapter -> each mode -> build the narration -> fetch the mp3,
and then the assertion the whole plan turns on: the narration that came back is
the source text. Real FFmpeg, real files, no network.
"""

import re

import pytest
from fastapi.testclient import TestClient

from videomaker.config import Settings
from videomaker.corpus.audio import Reading, reader_deps
from videomaker.corpus.importer import DERIVED_DIRNAME, library_dir
from videomaker.corpus.models import UnitRef
from videomaker.web.app import create_app

VERSE = re.compile(r'data-verse="(\d+)"')


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(workspace_dir=tmp_path / "workspace"), providers="mock")


def test_a_reader_can_read_a_chapter_three_ways_and_hear_the_source_text(app, tmp_path):
    with TestClient(app) as client:
        library = client.get("/library")
        assert library.status_code == 200
        assert "The Mock Work" in library.text

        outline = client.get("/library/mock")
        assert '/read/mock/JHN/1"' in outline.text

        source = client.get("/read/mock/JHN/1")
        assert source.status_code == 200
        verses = VERSE.findall(source.text)
        assert verses == ["1", "2", "3"]

        brief = client.get("/read/mock/JHN/1/mode/brief")
        assert brief.status_code == 200
        assert "A retelling" in brief.text

        listen = client.get("/read/mock/JHN/1/mode/listen")
        assert "Build the narration" in listen.text

        started = client.post("/read/mock/JHN/1/audio")
        assert started.status_code == 200
        assert app.state.jobs.wait_idle(120)

        player = client.get("/read/mock/JHN/1/mode/listen")
        assert "<audio" in player.text
        base = re.search(r'src="(/media/reading/mock/[^/]+)/reading\.mp3"', player.text)
        assert base is not None

        mp3 = client.get(f"{base.group(1)}/reading.mp3")
        assert mp3.status_code == 200
        assert mp3.headers["content-type"] == "audio/mpeg"
        assert len(mp3.content) > 1000

        vtt = client.get(f"{base.group(1)}/reading.vtt")
        assert vtt.status_code == 200
        assert vtt.text.count("-->") == len(verses)

    # The narration is the source text — the guarantee, read back off disk.
    key = base.group(1).rsplit("/", 1)[-1]
    reading_file = (
        library_dir(app.state.settings.workspace_dir)
        / "mock"
        / DERIVED_DIRNAME
        / "audio"
        / key
        / "reading.json"
    )
    reading = Reading.model_validate_json(reading_file.read_text())
    deps = reader_deps(app.state.settings, cache_dir=tmp_path / "cache")
    passage = deps.provider("corpus").unit(UnitRef(work_id="mock", book="JHN", chapter=1))
    spoken = " ".join(segment.text for segment in reading.segments)
    assert " ".join(spoken.split()) == " ".join(passage.plain.split())
