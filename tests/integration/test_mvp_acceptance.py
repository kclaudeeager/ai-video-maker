"""The definition of done for the MVP reader plan, in one test.

    import -> list -> outline -> read source -> build brief -> build reading -> fetch the mp3

against the two-book USFM fixture with `--providers mock`, driving the CLI and the
web app the way a person would, and ending on the assertion the whole plan turns
on: **the reassembled narration equals the fixture text.**

Everything here is offline. The corpus is the real `BibleCorpus` over a real
import, because the point is that the importer's output is what the reader reads;
only the model and the voice are mocked.
"""

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from videomaker import runner as runner_module
from videomaker.cli import app as cli
from videomaker.config import Settings
from videomaker.corpus import importer as importer_module
from videomaker.corpus.audio import Reading, reader_deps
from videomaker.corpus.importer import DERIVED_DIRNAME, library_dir
from videomaker.corpus.models import UnitRef
from videomaker.runner import PROVIDER_KINDS
from videomaker.web.app import create_app

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "usfm"
runner = CliRunner()

#: Every mode is mocked except the corpus: the reader has to read what the
#: importer actually wrote.
CHAINS = {**{kind: ["mock"] for kind in PROVIDER_KINDS}, "corpus": ["bible"]}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(importer_module, "NOTICE_PATH", tmp_path / "NOTICE.md")
    monkeypatch.setattr(runner_module, "USER_CACHE_DIR", tmp_path / "cache")
    return tmp_path


def test_the_mvp_acceptance_path(workspace):
    # ---------------------------------------------------------------- import
    imported = runner.invoke(
        cli, ["library", "import", "fixture", "--from", str(FIXTURES)]
    )
    assert imported.exit_code == 0, imported.output
    assert "Longhand Test Fixture" in imported.output

    # ------------------------------------------------------------------ list
    listed = runner.invoke(cli, ["library", "list"])
    assert listed.exit_code == 0
    assert "fixture" in listed.output
    assert "AGPL" in listed.output, "the licence is shown, because it is the condition"

    # ---------------------------------------------------------------- briefs
    briefed = runner.invoke(
        cli, ["library", "brief", "fixture", "--book", "JHN", "--providers", "mock"]
    )
    assert briefed.exit_code == 0, briefed.output
    assert "3 written" in briefed.output
    briefs = sorted(
        (workspace / "workspace" / "library" / "fixture" / DERIVED_DIRNAME / "brief").glob("*.json")
    )
    assert len(briefs) == 3
    assert json.loads(briefs[0].read_text())["summary"]

    # ---------------------------------------------------------------- doctor
    doctored = runner.invoke(cli, ["doctor"])
    assert doctored.exit_code == 0, doctored.output
    assert "FAIL" not in doctored.output
    assert "fixture" in doctored.output

    # ------------------------------------------------------------- in the browser
    settings = Settings(workspace_dir=workspace / "workspace", provider_chains=CHAINS)
    app = create_app(settings)
    with TestClient(app) as client:
        library = client.get("/library")
        assert library.status_code == 200
        assert "Longhand Test Fixture" in library.text

        outline = client.get("/library/fixture")
        assert outline.status_code == 200
        # Canonical order: Genesis before John, whatever the filesystem thinks.
        assert outline.text.index("Genesis") < outline.text.index("John")

        source = client.get("/read/fixture/JHN/3?mode=source")
        assert source.status_code == 200
        assert "There was a reader who came by night" in source.text

        # Chapter 1 is the one carrying a footnote, a cross-reference and a
        # section heading in the fixture; none of the three may reach the page a
        # narrator reads from.
        apparatus = client.get("/read/fixture/JHN/1?mode=source")
        assert "In the beginning of this fixture was a sentence" in apparatus.text
        for marker in ("FOOTNOTE-MARKER", "CROSSREF-MARKER", "A Heading That Must Not"):
            assert marker not in apparatus.text, "apparatus stays in source/"

        brief = client.get("/read/fixture/JHN/3/mode/brief")
        assert brief.status_code == 200
        assert "A retelling" in brief.text

        listen = client.get("/read/fixture/JHN/3/mode/listen")
        assert "Build the narration" in listen.text
        assert client.post("/read/fixture/JHN/3/audio").status_code == 200
        assert app.state.jobs.wait_idle(180)

        player = client.get("/read/fixture/JHN/3/mode/listen")
        base = re.search(r'src="(/media/reading/fixture/[^/]+)/reading\.mp3"', player.text)
        assert base is not None, player.text

        mp3 = client.get(f"{base.group(1)}/reading.mp3")
        assert mp3.status_code == 200
        assert mp3.headers["content-type"] == "audio/mpeg"
        assert len(mp3.content) > 1000

        vtt = client.get(f"{base.group(1)}/reading.vtt")
        assert vtt.status_code == 200

    # ------------------------------------- and the narration is the source text
    key = base.group(1).rsplit("/", 1)[-1]
    reading = Reading.model_validate_json(
        (
            library_dir(settings.workspace_dir)
            / "fixture"
            / DERIVED_DIRNAME
            / "audio"
            / key
            / "reading.json"
        ).read_text()
    )
    deps = reader_deps(settings, cache_dir=workspace / "cache")
    passage = deps.provider("corpus").unit(UnitRef(work_id="fixture", book="JHN", chapter=3))
    spoken = " ".join(segment.text for segment in reading.segments)

    assert " ".join(spoken.split()) == " ".join(passage.plain.split())
    assert vtt.text.count("-->") == len(passage.verses)
