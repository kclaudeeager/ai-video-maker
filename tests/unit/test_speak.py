"""Reading the passage aloud with the browser's own voice.

The fallback for a server with no voice of its own — the `ML=0` image has no
Kokoro and a 512 MB box could not hold it. It costs the server nothing: no model,
no file, no request.

`test_a_server_with_a_voice_does_not_offer_it` is the guarantee that matters. Two
ways to hear the same chapter, one of them worse and invisible in the feed and
the zip, is a choice nobody wants to be given.
"""

import pytest
from fastapi.testclient import TestClient

from videomaker.config import Audience, Settings
from videomaker.web.app import STATIC_DIR, create_app


@pytest.fixture
def voiceless(tmp_path, monkeypatch):
    """A reading server whose TTS provider offers nothing — the deployed case."""
    monkeypatch.setattr("videomaker.web.routes.library.spoken_languages", lambda settings: set())
    app = create_app(
        Settings(workspace_dir=tmp_path / "workspace", audience=Audience.READER),
        providers="mock",
    )
    with TestClient(app) as client:
        yield client


@pytest.fixture
def voiced(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "videomaker.web.routes.library.spoken_languages", lambda settings: {"en"}
    )
    app = create_app(
        Settings(workspace_dir=tmp_path / "workspace", audience=Audience.READER),
        providers="mock",
    )
    with TestClient(app) as client:
        yield client


def test_a_voiceless_server_offers_to_read_aloud(voiceless):
    body = voiceless.get("/read/mock/JHN/1").text

    assert 'id="speak-start"' in body
    assert "Read aloud" in body
    # Only this page pays for the script; `test_the_shelf_does_not_pay_for_it`
    # is the other half. Asserted here rather than in a test taking both
    # fixtures: they patch the same function, and the second one applied wins.
    assert '<script src="/static/speak.js"' in body


def test_the_control_starts_hidden_so_a_device_without_the_api_sees_no_button(voiceless):
    """`speak.js` unhides it after finding `speechSynthesis`. A button that does
    nothing is worse than no button."""
    body = voiceless.get("/read/mock/JHN/1").text

    assert 'id="speak" hidden' in body


def test_the_page_says_what_it_does_not_produce(voiceless):
    """No file means nothing to download, nothing for the feed, nothing in the
    zip. Better said on the page than discovered later."""
    body = voiceless.get("/read/mock/JHN/1").text

    assert "Nothing is generated or saved" in body


def test_it_can_record_the_place_like_the_real_player(voiceless):
    """The same hidden field the narration player posts to, so a verse reached by
    the browser's voice is remembered exactly as one reached by a reading."""
    body = voiceless.get("/read/mock/JHN/1").text

    assert 'id="reader-place"' in body
    assert 'value="/read/mock/JHN/1/place"' in body


def test_a_server_with_a_voice_does_not_offer_it(voiced):
    """Listen is strictly better where it exists: seekable, downloadable, in the
    feed and in the zip."""
    body = voiced.get("/read/mock/JHN/1").text

    assert 'id="speak-start"' not in body
    assert "speak.js" not in body


def test_the_script_is_served_as_javascript(voiceless):
    response = voiceless.get("/static/speak.js")

    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert "speechSynthesis" in response.text


def test_the_shelf_does_not_pay_for_it(voiceless):
    assert "speak.js" not in voiceless.get("/library").text


def test_it_speaks_the_documents_language_not_the_browsers():
    """A browser with several voices installed should not read French with an
    English mouth."""
    assert 'said.lang = document.documentElement.lang || "en"' in (
        STATIC_DIR / "speak.js"
    ).read_text()


def test_a_device_with_the_api_and_no_voice_is_told_so(voiceless):
    """Headless Chromium has `speechSynthesis` and zero voices, and so do some
    stripped Linux and Android builds. Without this the button presses, says
    nothing, and looks broken.

    The element is asserted here; that it fills in after 800 ms of silence was
    driven in a real browser, where the label reverted to "Read aloud" and the
    status read "This device has no speech voice installed."
    """
    body = voiceless.get("/read/mock/JHN/1").text

    assert 'id="speak-status"' in body
    assert 'role="status"' in body
    source = (STATIC_DIR / "speak.js").read_text()
    assert "speech.speaking || speech.pending" in source
    assert "no speech voice installed" in source


def test_the_button_stays_hidden_where_the_api_is_missing():
    """The panel ships `hidden`; only `speak.js` unhides it, and only after finding
    `speechSynthesis`. A device without the API — older Android WebViews, a
    hardened browser — must see no control at all rather than one that does
    nothing when pressed.

    Asserted against the source because pytest runs no JavaScript. It is a weaker
    check than exercising it, which is why the real browser pass exists; without
    it the guard can be deleted and every other test here still passes.
    """
    source = (STATIC_DIR / "speak.js").read_text()

    assert "|| !speech) return;" in source
    assert "panel.hidden = false;" in source
    assert source.index("!speech) return") < source.index("panel.hidden = false")
