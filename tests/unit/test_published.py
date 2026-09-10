"""Published: what a reading server shows, and what it will not admit exists.

Two rules carry this file.

**A work is private until its owner says otherwise.** Importing a text and
putting it in front of other people are two decisions, and the safe default for a
flag nobody has set is "not yet".

**An unpublished work is a 404, not a hidden row and not a 403.** A reader must
not be able to tell a private work from one that was never imported — a 403 is an
answer, and "does this exist?" is exactly the question not to answer.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from videomaker.cli import app as cli
from videomaker.config import Audience, Settings
from videomaker.corpus import importer as importer_module
from videomaker.corpus.catalogue import CATALOGUE
from videomaker.corpus.importer import import_work, list_works, read_work, set_published, work_dir
from videomaker.corpus.models import WorkRef
from videomaker.web.app import create_app

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "usfm"
CHAINS = {"corpus": ["bible"], "tts": ["mock"], "llm": ["mock"], "stock": ["mock"], "image": ["mock"]}
runner = CliRunner()


@pytest.fixture(autouse=True)
def _own_notice(tmp_path, monkeypatch):
    monkeypatch.setattr(importer_module, "NOTICE_PATH", tmp_path / "NOTICE.md")


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "workspace"
    import_work(CATALOGUE["fixture"].model_copy(update={"archive": str(FIXTURES)}), root)
    return root


def client_for(audience: Audience, workspace) -> TestClient:
    return TestClient(
        create_app(Settings(workspace_dir=workspace, audience=audience, provider_chains=CHAINS))
    )


# ---------------------------------------------------------------- the default


def test_a_work_is_private_until_its_owner_says_otherwise(workspace):
    (work, _chapters) = list_works(workspace)[0]

    assert work.published is False


def test_a_work_yaml_written_before_the_field_loads_unchanged():
    """Every `work.yaml` on disk predates this; none may need rewriting."""
    older = {
        "id": "w",
        "title": "W",
        "language": "en",
        "licence": "X.",
        "licence_url": "u",
        "source_url": "u",
        "versification": "kjv",
    }

    assert WorkRef.model_validate(older).published is False


def test_publishing_is_written_down_and_is_idempotent(workspace):
    assert set_published("fixture", True, workspace).published is True
    assert set_published("fixture", True, workspace).published is True
    assert read_work(work_dir(workspace, "fixture")).published is True

    assert set_published("fixture", False, workspace).published is False
    assert read_work(work_dir(workspace, "fixture")).published is False


def test_re_importing_does_not_quietly_unpublish(workspace):
    set_published("fixture", True, workspace)

    import_work(CATALOGUE["fixture"].model_copy(update={"archive": str(FIXTURES)}), workspace)

    assert read_work(work_dir(workspace, "fixture")).published is True


def test_publishing_changes_nothing_else_about_the_work(workspace):
    before = read_work(work_dir(workspace, "fixture"))

    after = set_published("fixture", True, workspace)

    assert after.model_dump(exclude={"published"}) == before.model_dump(exclude={"published"})


# ------------------------------------------------------------ what each sees


def test_the_studio_sees_everything(workspace):
    client = client_for(Audience.STUDIO, workspace)

    assert "Longhand Test Fixture" in client.get("/library").text
    assert client.get("/library/fixture").status_code == 200
    assert client.get("/read/fixture/JHN/1").status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "/library/fixture",
        "/read/fixture/JHN/1",
        "/read/fixture/JHN/1/mode/source",
        "/read/fixture/JHN/1/mode/brief",
        "/media/watch/fixture/JHN/1",
    ],
)
def test_an_unpublished_work_is_a_404_on_every_reader_route(workspace, path):
    """Not a 403: a reader must not learn that a private work exists."""
    response = client_for(Audience.READER, workspace).get(path)

    assert response.status_code == 404


def test_an_unpublished_work_cannot_be_posted_to_either(workspace):
    client = client_for(Audience.READER, workspace)

    assert client.post("/read/fixture/JHN/1/audio").status_code == 404
    assert client.post("/read/fixture/JHN/1/place", data={"verse": "1"}).status_code == 404


def test_an_unpublished_work_is_absent_from_the_shelf(workspace):
    body = client_for(Audience.READER, workspace).get("/library").text

    assert "Longhand Test Fixture" not in body
    assert "fixture" not in body
    assert "Nothing here yet" in body, "and the shelf reads as empty rather than filtered"


def test_publishing_makes_it_readable(workspace):
    set_published("fixture", True, workspace)
    client = client_for(Audience.READER, workspace)

    assert "Longhand Test Fixture" in client.get("/library").text
    assert client.get("/library/fixture").status_code == 200
    assert client.get("/read/fixture/JHN/1").status_code == 200


def test_taking_it_back_hides_it_again(workspace):
    set_published("fixture", True, workspace)
    assert client_for(Audience.READER, workspace).get("/read/fixture/JHN/1").status_code == 200

    set_published("fixture", False, workspace)

    assert client_for(Audience.READER, workspace).get("/read/fixture/JHN/1").status_code == 404


def test_the_corpus_itself_is_not_a_policy(workspace):
    """`BibleCorpus.works()` returns everything imported; the reader filters.

    A corpus that hid rows would make the studio unable to see its own library,
    and would put an audience rule in the layer that reads files off a disk.
    """
    from videomaker.corpus.bible import BibleCorpus

    corpus = BibleCorpus(Settings(workspace_dir=workspace, audience=Audience.READER))

    assert [work.id for work in corpus.works()] == ["fixture"]


# ------------------------------------------------------------------- the CLI


@pytest.fixture
def cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(importer_module, "NOTICE_PATH", tmp_path / "NOTICE.md")
    import_work(CATALOGUE["fixture"].model_copy(update={"archive": str(FIXTURES)}), Path("workspace"))
    return tmp_path


def test_the_cli_publishes_and_unpublishes(cwd):
    published = runner.invoke(cli, ["library", "publish", "fixture"])
    assert published.exit_code == 0, published.output
    assert "published" in published.output
    assert read_work(work_dir(Path("workspace"), "fixture")).published is True

    taken = runner.invoke(cli, ["library", "publish", "fixture", "--unpublish"])
    assert taken.exit_code == 0
    assert "private" in taken.output
    assert read_work(work_dir(Path("workspace"), "fixture")).published is False


def test_the_list_says_which_are_shown_to_readers(cwd):
    assert "private" in runner.invoke(cli, ["library", "list"]).output

    runner.invoke(cli, ["library", "publish", "fixture"])

    assert "published" in runner.invoke(cli, ["library", "list"]).output


def test_publishing_something_that_is_not_there_fails_plainly(cwd):
    result = runner.invoke(cli, ["library", "publish", "nope"])

    assert result.exit_code == 1
    assert "nope" in result.output
    assert "library list" in result.output
