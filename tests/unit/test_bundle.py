"""A whole work as one zip.

The feed is the subscription answer; this is the offline one, and the one that
outlives the server. Two guarantees carry the file.
`test_a_narrated_chapter_travels_byte_for_byte`: a bundle whose audio is not the
audio is worse than no bundle, because nobody checks. And
`test_the_licence_travels_with_the_work`: a redistributable text stops being
redistributable the moment it is separated from the sentence that says so, and
this is the one place the words leave the tool.
"""

import zipfile

import pytest
from fastapi.testclient import TestClient

from videomaker.config import Audience, Settings
from videomaker.corpus.audio import MP3_FILE, build_reading, reader_deps
from videomaker.corpus.bundle import (
    LICENCE_FILE,
    MAX_BUNDLE_BYTES,
    README_FILE,
    BundleTooLarge,
    bundle_name,
    bundle_size,
    write_bundle,
)
from videomaker.corpus.models import UnitRef, WorkRef
from videomaker.web.app import create_app

WORK = WorkRef(
    id="mock",
    title="The Mock Work",
    language="en",
    licence="Public domain.",
    licence_url="https://example.invalid/licence",
    source_url="https://example.invalid/source",
)


def app_for(audience: Audience, tmp_path):
    return create_app(
        Settings(workspace_dir=tmp_path / "workspace", audience=audience), providers="mock"
    )


def narrate(app, chapters, *, voice: str = "af_heart"):
    deps = reader_deps(app.state.settings)
    corpus = deps.provider("corpus")
    for chapter in chapters:
        unit = corpus.unit(UnitRef(work_id="mock", book="JHN", chapter=chapter))
        build_reading(unit, deps, voice=voice)
    return deps


def bundle_of(app, tmp_path, name="bundle.zip"):
    """Write the bundle straight from the module, and open it."""
    deps = reader_deps(app.state.settings)
    path = tmp_path / name
    write_bundle(path, WORK, settings=app.state.settings, corpus=deps.provider("corpus"))
    return zipfile.ZipFile(path)


# ------------------------------------------------------------------ the shape


def test_the_archive_is_valid(tmp_path):
    app = app_for(Audience.READER, tmp_path)
    narrate(app, [1])

    with bundle_of(app, tmp_path) as archive:
        assert archive.testzip() is None


def test_every_chapter_brings_its_text(tmp_path):
    """A work nobody has narrated still bundles — that is a book to read on a plane.

    Gating the download on narration would leave a deployment that cannot narrate
    at all, which is exactly what the free-tier image is, with nothing to offer.
    """
    app = app_for(Audience.READER, tmp_path)
    corpus = reader_deps(app.state.settings).provider("corpus")
    chapters = corpus.outline("mock")

    with bundle_of(app, tmp_path) as archive:
        texts = [name for name in archive.namelist() if name.startswith("text/")]

    assert len(texts) == len(chapters)


def test_the_text_keeps_its_verse_numbers(tmp_path):
    """`UnitText.plain` is what a narrator is given. A reader wants the numbers:
    they are how anybody refers to a line afterwards."""
    app = app_for(Audience.READER, tmp_path)
    corpus = reader_deps(app.state.settings).provider("corpus")
    unit = corpus.unit(UnitRef(work_id="mock", book="JHN", chapter=1))

    with bundle_of(app, tmp_path) as archive:
        first = next(name for name in sorted(archive.namelist()) if name.startswith("text/"))
        body = archive.read(first).decode()

    assert unit.title in body
    for verse in unit.verses:
        assert f"{verse.number}  {verse.text}" in body


def test_a_narrated_chapter_travels_byte_for_byte(tmp_path):
    """The audio in the zip is the audio on disk, not something re-encoded."""
    app = app_for(Audience.READER, tmp_path)
    deps = narrate(app, [1])
    corpus = deps.provider("corpus")
    from videomaker.corpus.feed import episodes_for

    episode = episodes_for(app.state.settings, "mock", corpus=corpus)[0]
    from videomaker.corpus.bundle import _audio_dir

    on_disk = (_audio_dir(app.state.settings, "mock", episode.key) / MP3_FILE).read_bytes()

    with bundle_of(app, tmp_path) as archive:
        packed = next(name for name in archive.namelist() if name.endswith(".mp3"))
        assert archive.read(packed) == on_disk


def test_a_narrated_chapter_brings_its_captions(tmp_path):
    app = app_for(Audience.READER, tmp_path)
    narrate(app, [1])

    with bundle_of(app, tmp_path) as archive:
        vtts = [name for name in archive.namelist() if name.endswith(".vtt")]

    assert len(vtts) == 1
    assert vtts[0].startswith("audio/")


def test_an_unnarrated_chapter_brings_no_audio(tmp_path):
    app = app_for(Audience.READER, tmp_path)
    narrate(app, [1])

    with bundle_of(app, tmp_path) as archive:
        audio = [name for name in archive.namelist() if name.endswith(".mp3")]

    assert len(audio) == 1


# ------------------------------------------------------------------- ordering


def test_entries_sort_into_reading_order(tmp_path):
    """File managers and players sort alphabetically, and `EXO` sorts before `GEN`.

    The number is the canonical position, so sorting the names gives the order the
    work is meant to be read in — the same problem `itunes:type=serial` solves for
    the feed, solved the same way.
    """
    app = app_for(Audience.READER, tmp_path)
    corpus = reader_deps(app.state.settings).provider("corpus")
    expected = [ref.chapter for ref in corpus.outline("mock")]

    with bundle_of(app, tmp_path) as archive:
        texts = sorted(name for name in archive.namelist() if name.startswith("text/"))

    assert [int(name.split("-")[-1].removesuffix(".txt")) for name in texts] == expected


def test_the_position_is_padded_wide_enough_for_a_bible(tmp_path):
    """1,189 chapters means a 1,000th entry, which a three-digit number would sort
    before the 999th — defeating the whole point of numbering them."""
    app = app_for(Audience.READER, tmp_path)

    with bundle_of(app, tmp_path) as archive:
        first = min(name for name in archive.namelist() if name.startswith("text/"))

    assert first.removeprefix("text/").startswith("0001-")


# ------------------------------------------------------------------- licensing


def test_the_licence_travels_with_the_work(tmp_path):
    app = app_for(Audience.READER, tmp_path)

    with bundle_of(app, tmp_path) as archive:
        licence = archive.read(LICENCE_FILE).decode()

    assert WORK.licence in licence
    assert WORK.licence_url in licence


def test_the_readme_says_what_this_is_and_who_may_use_it(tmp_path):
    app = app_for(Audience.READER, tmp_path)
    narrate(app, [1])

    with bundle_of(app, tmp_path) as archive:
        readme = archive.read(README_FILE).decode()

    assert WORK.title in readme
    assert WORK.licence in readme
    assert WORK.source_url in readme


# ---------------------------------------------------------------- compression


def test_audio_is_stored_and_text_is_deflated(tmp_path):
    """An mp3 is already compressed: deflating it spends CPU on every byte of the
    largest files in the archive to save under a percent. The text earns it."""
    app = app_for(Audience.READER, tmp_path)
    narrate(app, [1])

    with bundle_of(app, tmp_path) as archive:
        kinds = {
            info.filename.rsplit(".", 1)[-1]: info.compress_type for info in archive.infolist()
        }

    assert kinds["mp3"] == zipfile.ZIP_STORED
    assert kinds["txt"] == zipfile.ZIP_DEFLATED


# ------------------------------------------------------------------- the cap


def test_a_bundle_over_the_cap_is_refused_not_truncated(tmp_path, monkeypatch):
    """A bundle missing half a book, with nothing saying so, is worse than an error."""
    app = app_for(Audience.READER, tmp_path)
    narrate(app, [1])
    monkeypatch.setattr("videomaker.corpus.bundle.MAX_BUNDLE_BYTES", 1)
    corpus = reader_deps(app.state.settings).provider("corpus")

    with pytest.raises(BundleTooLarge):
        write_bundle(tmp_path / "over.zip", WORK, settings=app.state.settings, corpus=corpus)


def test_the_size_is_known_before_a_byte_is_written(tmp_path):
    app = app_for(Audience.READER, tmp_path)
    deps = narrate(app, [1])

    size = bundle_size(app.state.settings, "mock", corpus=deps.provider("corpus"))

    assert 0 < size < MAX_BUNDLE_BYTES


# -------------------------------------------------------------------- the route


def test_the_route_serves_a_zip(tmp_path):
    app = app_for(Audience.READER, tmp_path)
    narrate(app, [1])

    with TestClient(app) as client:
        response = client.get("/library/mock/bundle.zip")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert bundle_name(WORK) in response.headers["content-disposition"]
    assert response.content[:2] == b"PK"


def test_the_route_declares_its_length_so_a_download_can_show_progress(tmp_path):
    """Built to a temp file rather than streamed, precisely for this header: a
    chunked download of unknown length is the one people cancel at 80%."""
    app = app_for(Audience.READER, tmp_path)
    narrate(app, [1])

    with TestClient(app) as client:
        response = client.get("/library/mock/bundle.zip")

    assert int(response.headers["content-length"]) == len(response.content)


def test_the_route_leaves_no_temporary_file_behind(tmp_path):
    import tempfile
    from pathlib import Path

    app = app_for(Audience.READER, tmp_path)
    narrate(app, [1])
    before = set(Path(tempfile.gettempdir()).glob("mock-*.zip"))

    with TestClient(app) as client:
        client.get("/library/mock/bundle.zip")

    assert set(Path(tempfile.gettempdir()).glob("mock-*.zip")) == before


def test_an_unpublished_work_has_no_bundle_on_a_reading_server(tmp_path, monkeypatch):
    app = app_for(Audience.READER, tmp_path)
    monkeypatch.setattr(
        "videomaker.providers.mock.MockCorpus.works",
        lambda self: [WORK.model_copy(update={"published": False})],
    )

    with TestClient(app) as client:
        response = client.get("/library/mock/bundle.zip")

    assert response.status_code == 404


def test_the_work_page_offers_it(tmp_path):
    with TestClient(app_for(Audience.READER, tmp_path)) as client:
        body = client.get("/library/mock").text

    assert 'href="/library/mock/bundle.zip"' in body
