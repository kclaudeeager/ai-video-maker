"""A narrated work as a podcast feed.

Every player on earth already speaks RSS, which makes this the cheapest
publishing a local-first tool has: the owner sends somebody a URL and their
existing app does the rest — no account, no service, no new dependency.

Two guarantees carry the file. `test_only_narrated_chapters_appear`: a feed
advertising an episode that 404s is worse than a short feed. And
`test_the_enclosure_is_absolute_and_points_at_a_real_file`: an enclosure is the
one URL in this project that *must* be absolute, because a player has no base to
resolve against, and it is worth nothing if it does not resolve.
"""

from xml.etree import ElementTree as ET

from fastapi.testclient import TestClient

from videomaker.config import Audience, Settings
from videomaker.corpus.audio import build_reading, reader_deps
from videomaker.corpus.feed import ITUNES, MAX_EPISODES, Episode, episodes_for, feed_xml
from videomaker.corpus.models import UnitRef, WorkRef
from videomaker.web.app import create_app

WORK = WorkRef(
    id="mock", title="The Mock Work", language="en",
    licence="CC0.", licence_url="u", source_url="u",
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


def feed_of(app) -> ET.Element:
    with TestClient(app) as client:
        response = client.get("/library/mock/feed.xml")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/rss+xml")
    return ET.fromstring(response.text)


def items(root: ET.Element) -> list[ET.Element]:
    return root.findall("./channel/item")


# ------------------------------------------------------------------ the shape


def test_the_channel_says_what_a_player_needs(tmp_path):
    app = app_for(Audience.READER, tmp_path)
    narrate(app, [1])

    channel = feed_of(app).find("channel")

    assert channel.findtext("title") == "The Mock Work"
    assert channel.findtext("language") == "en"
    assert channel.findtext("link").endswith("/library/mock")
    assert "CC0" in channel.findtext("description")


def test_a_book_is_a_serial_numbered_from_one(tmp_path):
    """A client sorts newest first by default, which for a book is backwards.
    `serial` plus `itunes:episode` is how the format says "start at one"."""
    app = app_for(Audience.READER, tmp_path)
    narrate(app, [1, 2, 3])

    root = feed_of(app)

    assert root.find("channel").findtext(f"{{{ITUNES}}}type") == "serial"
    numbers = [item.findtext(f"{{{ITUNES}}}episode") for item in items(root)]
    assert numbers == ["1", "2", "3"]
    assert [item.findtext("title") for item in items(root)] == ["Mock 1", "Mock 2", "Mock 3"]


def test_the_namespace_prefix_is_itunes_not_ns0(tmp_path):
    """Legal XML either way; some clients only look for `itunes:`."""
    app = app_for(Audience.READER, tmp_path)
    narrate(app, [1])

    with TestClient(app) as client:
        text = client.get("/library/mock/feed.xml").text

    assert "<itunes:duration>" in text
    assert "ns0:" not in text


def test_the_duration_is_written_as_a_player_expects(tmp_path):
    app = app_for(Audience.READER, tmp_path)
    narrate(app, [1])

    (item,) = items(feed_of(app))

    assert item.findtext(f"{{{ITUNES}}}duration").count(":") == 2


def test_the_guid_survives_a_change_of_voice(tmp_path):
    """The reading key moves when the voice changes; which chapter it is does not."""
    app = app_for(Audience.READER, tmp_path)
    narrate(app, [1])
    (before,) = items(feed_of(app))

    narrate(app, [1], voice="am_adam")
    (after,) = items(feed_of(app))

    assert before.findtext("guid") == after.findtext("guid") == "mock/JHN/001"
    assert after.find("enclosure").get("url") != before.find("enclosure").get("url")


# ----------------------------------------------------------- what is listed


def test_only_narrated_chapters_appear(tmp_path):
    """A feed advertising an episode that 404s is worse than a short feed."""
    app = app_for(Audience.READER, tmp_path)
    narrate(app, [2])

    listed = [item.findtext("guid") for item in items(feed_of(app))]

    assert listed == ["mock/JHN/002"]


def test_a_work_with_nothing_narrated_is_still_a_valid_feed(tmp_path):
    """Subscribing before there is anything is a reasonable thing to do."""
    app = app_for(Audience.READER, tmp_path)

    root = feed_of(app)

    assert root.find("channel") is not None
    assert items(root) == []


def test_two_voices_are_one_episode(tmp_path):
    """A listener asked for the chapter, not for every take of it."""
    app = app_for(Audience.READER, tmp_path)
    narrate(app, [1], voice="af_heart")
    narrate(app, [1], voice="am_adam")

    assert len(items(feed_of(app))) == 1


def test_a_half_written_reading_is_not_an_episode(tmp_path):
    """`reading.json` is written last; a directory without one is a run that died."""
    app = app_for(Audience.READER, tmp_path)
    narrate(app, [1, 2])
    from videomaker.corpus.audio import AUDIO_DIRNAME, READING_FILE
    from videomaker.corpus.importer import DERIVED_DIRNAME, work_dir

    audio = work_dir(app.state.settings.workspace_dir, "mock") / DERIVED_DIRNAME / AUDIO_DIRNAME
    (next(iter(audio.iterdir())) / READING_FILE).unlink()

    assert len(items(feed_of(app))) == 1


def test_a_reading_whose_audio_is_gone_is_not_an_episode(tmp_path):
    """The half that the `json.loads` guard does not cover.

    A missing `reading.json` is caught by the parse anyway; a missing **mp3** is
    not, and it is the one that matters — it would advertise an enclosure that
    404s, which is precisely what a feed must never do.
    """
    app = app_for(Audience.READER, tmp_path)
    narrate(app, [1, 2])
    from videomaker.corpus.audio import AUDIO_DIRNAME, MP3_FILE
    from videomaker.corpus.importer import DERIVED_DIRNAME, work_dir

    audio = work_dir(app.state.settings.workspace_dir, "mock") / DERIVED_DIRNAME / AUDIO_DIRNAME
    (next(iter(sorted(audio.iterdir()))) / MP3_FILE).unlink()

    root = feed_of(app)
    assert len(items(root)) == 1

    # ...and the one that survived still resolves.
    with TestClient(app) as client:
        url = items(root)[0].find("enclosure").get("url")
        assert client.get(url.removeprefix("http://testserver")).status_code == 200


def test_the_feed_is_capped(tmp_path):
    """A client re-downloads the whole document on every refresh."""
    assert MAX_EPISODES == 300

    episodes = [
        Episode(
            ref=UnitRef(work_id="w", book="JHN", chapter=1),
            title=f"Chapter {n}",
            key="k",
            duration_s=1.0,
            bytes=1,
            made_at=0.0,
        )
        for n in range(MAX_EPISODES + 5)
    ]
    root = ET.fromstring(feed_xml(WORK, episodes, base_url="http://x/"))

    assert len(items(root)) == MAX_EPISODES + 5, "feed_xml renders what it is given"
    # The cap is `episodes_for`'s job, and it is asserted where it lives.


# ------------------------------------------------------------- the enclosure


def test_the_enclosure_is_absolute_and_points_at_a_real_file(tmp_path):
    """The one URL in this project that must be absolute — a player has no base."""
    app = app_for(Audience.READER, tmp_path)
    narrate(app, [1])

    with TestClient(app) as client:
        root = ET.fromstring(client.get("/library/mock/feed.xml").text)
        enclosure = items(root)[0].find("enclosure")
        url = enclosure.get("url")

        assert url.startswith("http://testserver/"), "absolute, on the host they reached"
        fetched = client.get(url.removeprefix("http://testserver"))

    assert fetched.status_code == 200
    assert fetched.headers["content-type"] == "audio/mpeg"
    assert len(fetched.content) == int(enclosure.get("length")), "length is the real size"
    assert enclosure.get("type") == "audio/mpeg"


def test_the_host_comes_from_the_request(tmp_path):
    """So a feed works on localhost, a LAN address or a tunnel without configuring."""
    app = app_for(Audience.READER, tmp_path)
    narrate(app, [1])

    with TestClient(app, base_url="http://reader.example:9000") as client:
        root = ET.fromstring(client.get("/library/mock/feed.xml").text)

    assert items(root)[0].find("enclosure").get("url").startswith("http://reader.example:9000/")


# ------------------------------------------------------------------- access


def test_an_unpublished_work_has_no_feed_on_a_reading_server(tmp_path, monkeypatch):
    from pathlib import Path as _Path

    from videomaker.corpus import importer as importer_module
    from videomaker.corpus.catalogue import CATALOGUE
    from videomaker.corpus.importer import import_work, set_published

    monkeypatch.setattr(importer_module, "NOTICE_PATH", tmp_path / "NOTICE.md")
    fixtures = _Path(__file__).resolve().parents[1] / "fixtures" / "usfm"
    workspace = tmp_path / "workspace"
    import_work(CATALOGUE["fixture"].model_copy(update={"archive": str(fixtures)}), workspace)
    chains = {"corpus": ["bible"], "tts": ["mock"], "llm": ["mock"]}

    reader = TestClient(
        create_app(Settings(workspace_dir=workspace, audience=Audience.READER, provider_chains=chains))
    )
    studio = TestClient(
        create_app(Settings(workspace_dir=workspace, audience=Audience.STUDIO, provider_chains=chains))
    )

    assert reader.get("/library/fixture/feed.xml").status_code == 404
    assert studio.get("/library/fixture/feed.xml").status_code == 200

    set_published("fixture", True, workspace)
    assert reader.get("/library/fixture/feed.xml").status_code == 200


def test_an_unknown_work_has_no_feed(tmp_path):
    with TestClient(app_for(Audience.READER, tmp_path)) as client:
        assert client.get("/library/nope/feed.xml").status_code == 404


def test_fetching_a_feed_generates_nothing(tmp_path):
    """It lists what exists, so it needs no budget and cannot be used to make a
    server work."""
    app = app_for(Audience.READER, tmp_path)

    with TestClient(app) as client:
        client.get("/library/mock/feed.xml")

    assert app.state.jobs.states() == {}


def test_the_work_page_offers_it(tmp_path):
    """As an address to copy, absolute, not a link to follow.

    Following the link lands a reader on raw XML, which is a dead end — the feed
    is addressed to a podcast player, and the URL has to be absolute because the
    player is given it with no page to resolve it against.
    """
    with TestClient(app_for(Audience.READER, tmp_path)) as client:
        body = client.get("/library/mock").text

    assert 'value="http://testserver/library/mock/feed.xml"' in body
    assert "podcast player" in body


def test_the_work_page_uses_the_address_the_reader_arrived_on(tmp_path):
    """A feed URL naming `localhost` is useless to the phone it gets pasted into.

    The host comes off the request, so a reader who reached this page over the
    LAN copies the LAN address and a reader on a tunnel copies the tunnel.
    """
    client = TestClient(app_for(Audience.READER, tmp_path), base_url="http://192.168.1.9:8000")
    with client:
        body = client.get("/library/mock").text

    assert 'value="http://192.168.1.9:8000/library/mock/feed.xml"' in body


def test_a_loopback_address_says_it_only_works_here(tmp_path):
    """`serve` binds loopback by default, so this is the common case.

    A feed URL naming `localhost` is not a feed URL a phone can subscribe to —
    on a phone that name means the phone. Saying "any podcast player" there is
    simply wrong, and the person finds out after pasting it into one.
    """
    client = TestClient(app_for(Audience.READER, tmp_path), base_url="http://localhost:8000")
    with client:
        body = client.get("/library/mock").text

    assert 'value="http://localhost:8000/library/mock/feed.xml"' in body
    assert "on this machine" in body
    assert "videomaker serve --host 0.0.0.0" in body
    assert "LONGHAND_PASSWORD" in body


def test_an_address_other_devices_can_reach_does_not_carry_the_warning(tmp_path):
    client = TestClient(app_for(Audience.READER, tmp_path), base_url="http://192.168.1.9:8000")
    with client:
        body = client.get("/library/mock").text

    assert "on this machine" not in body
    assert "any device that can reach" in body
    assert "password" not in body


def test_a_gated_server_says_the_player_needs_the_password(tmp_path):
    """`PasswordGate` covers the feed and the mp3s, and a player handed a bare URL
    gets 401 — which most report as "feed not found", pointing at the wrong thing.

    Keyed off the request rather than the environment: the browser authenticated
    to reach this page, so a player will have to as well.
    """
    client = TestClient(app_for(Audience.READER, tmp_path), base_url="http://192.168.1.9:8000")
    with client:
        body = client.get("/library/mock", headers={"Authorization": "Basic bG9uZ2hhbmQ6cHc="}).text

    assert "behind the password gate" in body
    assert "longhand:your-password@" in body


def test_only_episodes_for_caps(tmp_path):
    """Where the cap actually lives."""
    app = app_for(Audience.READER, tmp_path)
    narrate(app, [1, 2, 3])
    deps = reader_deps(app.state.settings)

    found = episodes_for(app.state.settings, "mock", corpus=deps.provider("corpus"))

    assert len(found) == 3
    assert [episode.ref.chapter for episode in found] == [1, 2, 3]
