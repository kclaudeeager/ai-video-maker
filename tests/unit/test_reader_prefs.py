"""The reader's preference: which mode, in a signed cookie.

It is a cookie and not a `Settings` field because `Settings` is machine-wide while
this is per person — two readers on one self-hosted instance must not overwrite
each other's choice. The signature is not protecting a secret; it is what makes a
tampered value fall back to the default instead of reaching `ReadMode`.
"""

import pytest
from fastapi.testclient import TestClient

from videomaker.config import Settings
from videomaker.web.app import create_app
from videomaker.web.routes.library import (
    DEFAULT_MODE,
    PREFS_COOKIE,
    ReadMode,
    decode_prefs,
    encode_prefs,
)

SECRET = "a-fixed-secret-for-the-tests"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(workspace_dir=tmp_path / "workspace", reader_cookie_secret=SECRET)


@pytest.fixture
def app(settings):
    return create_app(settings, providers="mock")


@pytest.fixture
def client(app):
    return TestClient(app)


# ------------------------------------------------------------------ the value


def test_the_cookie_round_trips(settings):
    raw = encode_prefs(settings, mode="listen", language="en")
    assert decode_prefs(settings, raw) == (ReadMode.LISTEN, "en")


def test_the_default_mode_is_the_brief():
    assert DEFAULT_MODE is ReadMode.BRIEF


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "nonsense",
        "no-signature.",
        ".only-a-signature",
        "bm90LWEtbW9kZQ.deadbeef",
        "!!!!.!!!!",
    ],
)
def test_a_cookie_that_is_not_one_falls_back_rather_than_raising(settings, raw):
    assert decode_prefs(settings, raw) == (DEFAULT_MODE, "")


def test_a_tampered_payload_falls_back(settings):
    raw = encode_prefs(settings, mode="listen", language="en")
    payload, _, signature = raw.partition(".")
    forged = encode_prefs(settings, mode="source", language="en").partition(".")[0]
    assert decode_prefs(settings, f"{forged}.{signature}") == (DEFAULT_MODE, "")
    assert decode_prefs(settings, f"{payload}.{signature[:-2]}xx") == (DEFAULT_MODE, "")


def test_a_cookie_signed_with_another_secret_falls_back(settings, tmp_path):
    other = Settings(workspace_dir=tmp_path, reader_cookie_secret="a-different-secret")
    raw = encode_prefs(other, mode="listen", language="en")
    assert decode_prefs(settings, raw) == (DEFAULT_MODE, "")
    assert decode_prefs(other, raw) == (ReadMode.LISTEN, "en")


def test_an_unknown_mode_in_a_correctly_signed_cookie_falls_back(settings):
    # Correctly signed and still refused: a cookie written by a build that had a
    # fourth mode must not reach `ReadMode`.
    raw = encode_prefs(settings, mode="interpretive-dance", language="en")
    assert decode_prefs(settings, raw) == (DEFAULT_MODE, "")


def test_an_empty_secret_still_signs(tmp_path):
    """The default is a per-process secret, not no signature at all."""
    settings = Settings(workspace_dir=tmp_path)
    raw = encode_prefs(settings, mode="source", language="en")
    assert decode_prefs(settings, raw) == (ReadMode.SOURCE, "en")
    assert decode_prefs(settings, raw.partition(".")[0] + ".x") == (DEFAULT_MODE, "")


# ------------------------------------------------------------------ in the app


def test_a_first_visit_gets_the_default_mode_and_is_remembered(client):
    response = client.get("/read/mock/JHN/1")
    assert response.status_code == 200
    assert "A retelling" in response.text
    assert PREFS_COOKIE in response.cookies


def test_switching_mode_is_remembered_for_the_next_chapter(client):
    client.get("/read/mock/JHN/1/mode/source")
    assert client.cookies.get(PREFS_COOKIE)
    later = client.get("/read/mock/JHN/2")
    assert "A retelling" not in later.text
    assert 'data-verse="1"' in later.text


def test_an_explicit_mode_in_the_url_beats_the_cookie(client):
    client.get("/read/mock/JHN/1/mode/source")
    response = client.get("/read/mock/JHN/2?mode=brief")
    assert "A retelling" in response.text


def test_a_preference_the_work_cannot_support_does_not_break_the_page(client, monkeypatch):
    client.get("/read/mock/JHN/1/mode/listen")
    from videomaker.web.routes import library as library_routes

    monkeypatch.setattr(library_routes, "spoken_languages", lambda settings: set())
    response = client.get("/read/mock/JHN/2")
    assert response.status_code == 200
    assert "A retelling" in response.text
    assert ">Listen<" not in response.text


def test_a_forged_cookie_reaching_the_app_is_ignored(client, settings):
    client.cookies.set(PREFS_COOKIE, "bm90LWEtbW9kZQ.forged")
    response = client.get("/read/mock/JHN/1")
    assert response.status_code == 200
    assert "A retelling" in response.text


def test_the_reading_page_declares_the_work_language(client):
    assert '<html lang="en">' in client.get("/read/mock/JHN/1").text
