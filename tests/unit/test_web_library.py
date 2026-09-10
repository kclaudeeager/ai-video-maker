"""The reader's routes: every one on the mock chain, and the media guard.

`test_a_traversal_in_the_reading_key_reads_nothing` is the important one. The
reading key is a hash in normal use and an arbitrary string in a hostile one, so
the media route hands it to `web/media.py`'s guard — the same one the project
media route has used since M2 Task 2 — and every rejection is the same 404.

The mode switcher is the other thing under test: a work whose language no
configured voice speaks has **no** Listen control, rather than one that fails when
pressed (`docs/multimodal-reader-design.md` §6).
"""

from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from videomaker.config import Settings
from videomaker.corpus import importer as importer_module
from videomaker.corpus.audio import AUDIO_DIRNAME, build_reading, reader_deps, reading_key
from videomaker.corpus.catalogue import CATALOGUE
from videomaker.corpus.importer import DERIVED_DIRNAME, import_work, library_dir
from videomaker.corpus.models import UnitRef
from videomaker.runner import PROVIDER_KINDS
from videomaker.web.app import create_app
from videomaker.web.routes.library import ReadMode, modes_for, neighbours, spoken_languages

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "usfm"


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(workspace_dir=tmp_path / "workspace"), providers="mock")


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def real_library(tmp_path, monkeypatch):
    """An app over a real import, with every *network* provider mocked.

    `--providers mock` swaps the corpus too, which is right for a route test and
    wrong for one about a work on disk — so this builds the chains by hand.
    """
    monkeypatch.setattr(importer_module, "NOTICE_PATH", tmp_path / "NOTICE.md")
    workspace = tmp_path / "workspace"
    import_work(CATALOGUE["fixture"].model_copy(update={"archive": str(FIXTURES)}), workspace)
    settings = Settings(
        workspace_dir=workspace,
        provider_chains={**{kind: ["mock"] for kind in PROVIDER_KINDS}, "corpus": ["bible"]},
    )
    return create_app(settings), settings


# ------------------------------------------------------------------- the pages


def test_the_library_page_shows_every_work_with_its_licence(client):
    response = client.get("/library")
    assert response.status_code == 200
    assert "The Mock Work" in response.text
    assert "CC0 (synthetic test fixture" in response.text


def test_the_shelf_says_what_you_can_do_rather_than_labelling_it(client):
    """A chip reading `SOURCE BRIEF LISTEN` is a legend; a sentence is an answer
    (`docs/ui-design.md` §12), and the all-caps chips were one of the four
    treatments that section bans."""
    body = client.get("/library").text

    assert "Read it, skim it, or hear it" in body
    assert "SOURCE" not in body and "LISTEN" not in body
    assert "·" not in body, "no middle-dot meta strings"


def test_a_work_with_no_voice_says_what_it_can_still_do(app, monkeypatch):
    from videomaker.web.routes import library as library_routes

    monkeypatch.setattr(library_routes, "spoken_languages", lambda settings: set())
    body = TestClient(app).get("/library").text

    assert "Read it or skim it" in body
    assert "hear it" not in body


def test_an_empty_library_offers_both_ways_to_fill_it(app, tmp_path):
    empty = create_app(
        Settings(
            workspace_dir=tmp_path / "nothing",
            provider_chains={"corpus": ["bible"], "tts": ["mock"], "llm": ["mock"]},
        )
    )
    body = TestClient(empty).get("/library").text

    assert 'href="/start/read"' in body
    assert 'href="/start/document"' in body


def test_an_empty_library_says_so_rather_than_erroring(real_library, tmp_path):
    empty = create_app(
        Settings(
            workspace_dir=tmp_path / "nothing",
            provider_chains={"corpus": ["bible"], "tts": ["mock"], "llm": ["mock"]},
        )
    )
    response = TestClient(empty).get("/library")
    assert response.status_code == 200
    assert "Nothing here yet" in response.text


def test_the_work_page_lists_books_and_chapters(client):
    response = client.get("/library/mock")
    assert response.status_code == 200
    assert "John" in response.text
    assert '/read/mock/JHN/3"' in response.text


def test_an_unknown_work_is_a_404(client):
    assert client.get("/library/nope").status_code == 404
    assert client.get("/read/nope/JHN/1").status_code == 404


def test_the_reading_page_renders_the_verses(client):
    response = client.get("/read/mock/JHN/2")
    assert response.status_code == 200
    assert 'data-verse="1"' in response.text
    assert "invented sentence 1 of chapter 2" in response.text


@pytest.mark.parametrize("mode", ["source", "brief", "listen"])
def test_every_mode_renders_as_a_page_and_as_a_fragment(client, mode):
    page = client.get(f"/read/mock/JHN/2?mode={mode}")
    assert page.status_code == 200
    fragment = client.get(f"/read/mock/JHN/2/mode/{mode}")
    assert fragment.status_code == 200
    assert fragment.text.lstrip().startswith('<div id="reader"')
    assert "<html" not in fragment.text


def test_the_brief_is_labelled_a_retelling(client):
    response = client.get("/read/mock/JHN/2/mode/brief")
    assert "A retelling" in response.text
    assert "tap a verse for the text" in response.text


def test_an_unknown_mode_is_a_404(client):
    assert client.get("/read/mock/JHN/2/mode/interpretive-dance").status_code == 404


# ------------------------------------------------------------- routing guards


@pytest.mark.parametrize("book", ["ZZZ", "..", "%2e%2e", "etc"])
def test_a_book_outside_the_canon_is_a_404(client, book):
    assert client.get(f"/read/mock/{book}/1").status_code == 404


def test_a_chapter_that_is_not_a_number_is_a_404(client):
    assert client.get("/read/mock/JHN/abc").status_code == 404
    assert client.get("/read/mock/JHN/0").status_code == 404


def test_the_previous_and_next_chapters_cross_book_boundaries():
    refs = [
        UnitRef(work_id="w", book="GEN", chapter=1),
        UnitRef(work_id="w", book="GEN", chapter=2),
        UnitRef(work_id="w", book="JHN", chapter=1),
    ]
    previous, following = neighbours(refs, refs[1])
    assert previous.key() == "w/GEN/001"
    assert following.key() == "w/JHN/001"
    assert neighbours(refs, refs[0])[0] is None
    assert neighbours(refs, refs[-1])[1] is None


# ------------------------------------------------------------------ the modes


def test_listen_is_offered_when_a_voice_speaks_the_language(client):
    response = client.get("/read/mock/JHN/2")
    assert ">Listen<" in response.text


def test_a_work_with_no_voice_for_its_language_has_no_listen_control(app, monkeypatch):
    from videomaker.web.routes import library as library_routes

    monkeypatch.setattr(library_routes, "spoken_languages", lambda settings: {"rw"})
    response = TestClient(app).get("/read/mock/JHN/2")
    assert response.status_code == 200
    assert ">Listen<" not in response.text
    assert ">Read<" in response.text and ">Brief<" in response.text
    # ...and asking for it directly is a 404, not a broken page.
    assert TestClient(app).get("/read/mock/JHN/2/mode/listen").status_code == 404
    assert TestClient(app).post("/read/mock/JHN/2/audio").status_code == 404


def test_spoken_languages_reads_the_configured_chain(app):
    assert "en" in spoken_languages(app.state.settings)


def test_a_vendor_states_its_own_languages(tmp_path):
    settings = Settings(
        workspace_dir=tmp_path,
        provider_chains={"tts": ["vendor"]},
        voice_providers=[
            {
                "name": "vendor",
                "endpoint": "https://voice.invalid/speak",
                "languages": ["rw", "sw"],
                "voices": ["nyira"],
            }
        ],
    )
    assert spoken_languages(settings) == {"rw", "sw"}


def test_modes_for_always_offers_the_source_and_the_way_out(tmp_path):
    """A language with no voice can be read and skimmed, and can still be *made*:
    Watch needs no voice for the work's language, because the project it starts is
    narrated in whatever the pipeline is configured for and gate 1 is where that
    gets decided."""
    from videomaker.corpus.models import WorkRef

    work = WorkRef(
        id="w", title="W", language="rw", licence="X.", licence_url="u", source_url="u"
    )
    settings = Settings(workspace_dir=tmp_path, provider_chains={"tts": []})
    assert modes_for(work, settings) == [ReadMode.SOURCE, ReadMode.BRIEF, ReadMode.WATCH]


# ------------------------------------------------------------- building audio


def test_building_the_reading_goes_through_the_job_queue(app):
    with TestClient(app) as client:
        response = client.post("/read/mock/JHN/2/audio")
        assert response.status_code == 200
        assert "Synthesising" in response.text
        app.state.jobs.wait_idle(30)
        state = app.state.jobs.state_for("reading:mock/JHN/002:af_heart")
        assert state is not None
        assert state.kind == "reading"
        assert state.state == "done", state.error


def test_the_player_appears_once_the_reading_exists(app):
    with TestClient(app) as client:
        client.post("/read/mock/JHN/2/audio")
        app.state.jobs.wait_idle(30)
        response = client.get("/read/mock/JHN/2/mode/listen")
    assert "<audio" in response.text
    assert 'kind="metadata"' in response.text
    assert "/media/reading/mock/" in response.text


# -------------------------------------------------------------- the media route


@pytest.fixture
def served(app, tmp_path):
    """One real reading on disk, and the key it was written under."""
    settings = app.state.settings
    deps = reader_deps(settings, cache_dir=tmp_path / "cache")
    unit = deps.provider("corpus").unit(UnitRef(work_id="mock", book="JHN", chapter=1))
    build_reading(unit, deps, voice="af_heart")
    return reading_key(unit, provider="mock", voice="af_heart", speed=1.0)


def test_the_mp3_and_the_vtt_are_served(client, served):
    mp3 = client.get(f"/media/reading/mock/{served}/reading.mp3")
    assert mp3.status_code == 200
    assert mp3.headers["content-type"] == "audio/mpeg"
    vtt = client.get(f"/media/reading/mock/{served}/reading.vtt")
    assert vtt.status_code == 200
    assert vtt.text.startswith("WEBVTT")


def test_nothing_else_in_the_directory_is_served(client, served):
    assert client.get(f"/media/reading/mock/{served}/reading.json").status_code == 404
    assert client.get(f"/media/reading/mock/{served}/concat.txt").status_code == 404


@pytest.mark.parametrize(
    "key",
    [
        "../../../../etc",
        "..%2f..%2fwork",
        quote("../../work", safe=""),
        "....//....//work",
        "/etc",
        quote("/etc/passwd", safe=""),
    ],
)
def test_a_traversal_in_the_reading_key_reads_nothing(client, served, key, planted):
    response = client.get(f"/media/reading/mock/{key}/reading.mp3")
    assert response.status_code == 404
    assert planted.is_file(), "the escape target must still exist for this to prove anything"


@pytest.fixture
def planted(app, tmp_path) -> Path:
    """A real `reading.mp3` one level *outside* the served directory.

    Without it a traversal test proves only that the escape target happens to be
    empty — the guard could be gone and every request would still 404 for the
    wrong reason. With it, a missing containment check is a 200.
    """
    audio_root = (
        library_dir(app.state.settings.workspace_dir) / "mock" / DERIVED_DIRNAME / AUDIO_DIRNAME
    )
    audio_root.mkdir(parents=True, exist_ok=True)
    target = audio_root.parent / "reading.mp3"
    target.write_bytes(b"not yours")
    (tmp_path / "reading.mp3").write_bytes(b"not yours either")
    return target


def test_the_planted_file_is_reachable_by_escaping_and_only_by_escaping(client, served, planted):
    # The escape that the guard has to refuse, spelled out: one level up from the
    # served directory, where `planted` really is.
    assert client.get("/media/reading/mock/..%2f/reading.mp3").status_code == 404
    assert client.get("/media/reading/mock/%2e%2e/reading.mp3").status_code == 404
    assert planted.read_bytes() == b"not yours"


def test_a_symlink_out_of_the_directory_is_refused(client, served, tmp_path, app):
    audio_root = (
        library_dir(app.state.settings.workspace_dir) / "mock" / DERIVED_DIRNAME / AUDIO_DIRNAME
    )
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"not yours")
    escape = audio_root / "escape"
    escape.mkdir(parents=True, exist_ok=True)
    (escape / "reading.mp3").symlink_to(outside)
    assert client.get("/media/reading/mock/escape/reading.mp3").status_code == 404


def test_a_traversal_in_the_work_id_reads_nothing(client, served):
    assert client.get(f"/media/reading/../../mock/{served}/reading.mp3").status_code == 404


def test_a_missing_reading_is_a_404_not_an_error(client):
    assert client.get("/media/reading/mock/deadbeefdeadbeef/reading.mp3").status_code == 404


# ------------------------------------------------------------- the real library


def test_a_real_import_is_readable_end_to_end(real_library):
    app, _settings = real_library
    client = TestClient(app)
    assert "Longhand Test Fixture" in client.get("/library").text
    assert client.get("/library/fixture").status_code == 200
    page = client.get("/read/fixture/JHN/3")
    assert page.status_code == 200
    assert "There was a reader who came by night" in page.text
    assert "FOOTNOTE-MARKER" not in page.text


def test_the_nav_links_to_the_library(client):
    assert '/library"' in client.get("/library").text


# ------------------------------------------------------- a document of your own


@pytest.fixture
def document_app(tmp_path, monkeypatch):
    """A work made from a Markdown file, served by the real corpus."""
    from videomaker.corpus.documents import DocumentSpec, import_document

    monkeypatch.setattr(importer_module, "NOTICE_PATH", tmp_path / "NOTICE.md")
    source = tmp_path / "field-notes.md"
    source.write_text("# The first part\n\nA paragraph of prose.\n\nAnd another.\n")
    workspace = tmp_path / "workspace"
    import_document(source, DocumentSpec(work_id="notes", title="Field Notes"), workspace)
    settings = Settings(
        workspace_dir=workspace,
        provider_chains={**{kind: ["mock"] for kind in PROVIDER_KINDS}, "corpus": ["bible"]},
    )
    return create_app(settings)


def test_a_document_reads_like_any_other_work(document_app):
    client = TestClient(document_app)
    assert "Field Notes" in client.get("/library").text

    page = client.get("/read/notes/DOC/1")
    assert page.status_code == 200
    assert "A paragraph of prose." in page.text
    assert "The first part" in page.text


def test_a_document_keeps_its_paragraph_handles_but_not_its_numbers(document_app):
    """The number is how a cue and a click find a paragraph; it is not text."""
    body = TestClient(document_app).get("/read/notes/DOC/1").text

    assert 'data-verse="1"' in body, "reader.js still needs the handle"
    assert '<sup class="verse-num"' not in body, "a made-up number is not part of the document"


def test_a_versified_work_still_shows_its_verse_numbers(client):
    body = client.get("/read/mock/JHN/1").text

    assert '<sup class="verse-num"' in body
    assert 'data-verse="1"' in body


def test_a_document_offers_the_same_three_modes(document_app):
    body = TestClient(document_app).get("/read/notes/DOC/1").text
    for label in (">Read<", ">Brief<", ">Listen<"):
        assert label in body


def test_every_mode_has_a_label(client):
    """A mode in the enum and not in the label table renders as an empty tab —
    which is what a Jinja lookup miss looks like, and what `watch` shipped as
    until a browser showed a blank gap in the switcher."""
    from videomaker.web.routes.library import MODE_LABELS

    assert set(MODE_LABELS) == set(ReadMode)
    assert all(label for label in MODE_LABELS.values())

    body = client.get("/read/mock/JHN/1").text
    for label in MODE_LABELS.values():
        assert f">{label}</a>" in body
