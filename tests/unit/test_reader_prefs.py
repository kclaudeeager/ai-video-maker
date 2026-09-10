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
    MAX_BOOKMARKS,
    PREFS_COOKIE,
    Bookmark,
    ReadMode,
    decode_places,
    decode_prefs,
    encode_prefs,
    with_place,
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


# ---------------------------------------------------------------- where you were
#
# Per work, not one global slot: someone reading two books at once should not have
# them fight. In the same signed cookie as the mode, because it is the same kind
# of fact — per person, not per machine.


def a_place(work_id="web", chapter="003", verse=0, title="John 3") -> Bookmark:
    return Bookmark(
        work_id=work_id, unit_key=f"{work_id}/JHN/{chapter}", title=title, verse=verse
    )


def test_a_place_round_trips(settings):
    raw = encode_prefs(settings, mode="listen", language="en", places=[a_place(verse=12)])

    (back,) = decode_places(settings, raw)
    assert back == a_place(verse=12)
    assert back.href == "/read/web/JHN/3#v12"
    # ...and the mode still reads back beside it.
    assert decode_prefs(settings, raw) == (ReadMode.LISTEN, "en")


def test_a_place_at_the_top_of_a_unit_carries_no_anchor():
    assert a_place(verse=0).href == "/read/web/JHN/3"


def test_each_work_keeps_its_own_place(settings):
    places = with_place(with_place([], a_place("web")), a_place("bsb", title="John 3"))
    raw = encode_prefs(settings, mode="source", places=places)

    got = {place.work_id: place.unit_key for place in decode_places(settings, raw)}
    assert got == {"web": "web/JHN/003", "bsb": "bsb/JHN/003"}


def test_returning_to_a_work_moves_its_place_rather_than_adding_one(settings):
    places = with_place([], a_place("web", chapter="003"))
    places = with_place(places, a_place("web", chapter="004", title="John 4"))

    assert [place.unit_key for place in places] == ["web/JHN/004"]


def test_the_oldest_place_falls_off_the_end():
    places: list[Bookmark] = []
    for index in range(MAX_BOOKMARKS + 3):
        places = with_place(places, a_place(work_id=f"work{index}"))

    assert len(places) == MAX_BOOKMARKS
    assert places[0].work_id == f"work{MAX_BOOKMARKS + 2}", "newest first"
    assert "work0" not in {place.work_id for place in places}


@pytest.mark.parametrize("raw", ["", "nonsense", "bm90LWEtbW9kZQ.forged", "x.y"])
def test_an_unreadable_cookie_loses_the_place_rather_than_raising(settings, raw):
    assert decode_places(settings, raw) == []


def test_a_tampered_cookie_loses_the_place(settings):
    raw = encode_prefs(settings, mode="source", places=[a_place()])
    payload, _, signature = raw.partition(".")

    assert decode_places(settings, f"{payload}xx.{signature}") == []


def test_a_row_that_is_not_a_place_is_dropped_not_raised(settings):
    good = encode_prefs(settings, mode="source", places=[a_place()])
    # A cookie from a build that wrote its rows differently.
    from base64 import urlsafe_b64decode, urlsafe_b64encode

    payload = good.partition(".")[0]
    decoded = urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode()
    broken = decoded + "~not-a-place~web/JHN/xxx:0:Bad"
    from videomaker.web.routes.library import _sign

    mangled = urlsafe_b64encode(broken.encode()).decode().rstrip("=")
    raw = f"{mangled}.{_sign(mangled, settings)}"

    assert [place.unit_key for place in decode_places(settings, raw)] == ["web/JHN/003"]


def test_a_title_with_a_separator_in_it_cannot_corrupt_the_cookie(settings):
    raw = encode_prefs(
        settings, mode="source", places=[a_place(title="Genesis 1: in the beginning ~ etc")]
    )

    (back,) = decode_places(settings, raw)
    assert back.unit_key == "web/JHN/003"
    assert ":" not in back.title and "~" not in back.title


# ------------------------------------------------------------------ in the app


def test_opening_a_chapter_records_where_you_were(client):
    client.get("/read/mock/JHN/2")

    (place,) = decode_places(
        client.app.state.settings, client.cookies.get(PREFS_COOKIE)
    )
    assert place.unit_key == "mock/JHN/002"
    assert place.title == "Mock 2"


def test_switching_mode_does_not_erase_the_place(client):
    client.get("/read/mock/JHN/2")
    client.get("/read/mock/JHN/2/mode/brief")

    places = decode_places(client.app.state.settings, client.cookies.get(PREFS_COOKIE))
    assert [place.unit_key for place in places] == ["mock/JHN/002"]


def test_the_player_can_record_the_verse_it_reached(client):
    client.get("/read/mock/JHN/2")
    response = client.post("/read/mock/JHN/2/place", data={"verse": "3"})

    assert response.status_code == 204
    (place,) = decode_places(client.app.state.settings, client.cookies.get(PREFS_COOKIE))
    assert place.verse == 3
    assert place.href.endswith("#v3")


def test_recording_a_place_in_a_chapter_that_is_not_there_is_a_404(client):
    assert client.post("/read/mock/JHN/99/place", data={"verse": "1"}).status_code == 404
    assert client.post("/read/mock/ZZZ/1/place", data={"verse": "1"}).status_code == 404


def test_the_library_offers_to_pick_a_work_back_up(client):
    assert "Pick up" not in client.get("/library").text

    client.get("/read/mock/JHN/2")
    body = client.get("/library").text

    assert "Pick up Mock 2" in body
    assert 'href="/read/mock/JHN/2"' in body
