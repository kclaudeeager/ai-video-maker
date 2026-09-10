"""What a reading server says, and what it never asks.

Two guarantees carry this file.

`test_a_reader_is_never_asked_to_spend_the_owners_money` is the one that would be
expensive to get wrong: where a paid voice is over budget the studio shows the
estimate and a confirm button, because the person looking at it owns the account.
A reader does not, so they are told the narration is unavailable instead of being
invited to authorise a bill on somebody else's card.

`test_opening_listen_twice_enqueues_one_job` is the one that would be expensive
in a quieter way: a reading page that requests on sight, and polls itself every
1.5 s, is one careless line away from requesting on every poll.
"""

import pytest
from fastapi.testclient import TestClient

from videomaker.config import Audience, Settings
from videomaker.web.app import create_app
from videomaker.web.routes.library import ReadMode, modes_for


def app_for(audience: Audience, tmp_path, **overrides):
    return create_app(
        Settings(workspace_dir=tmp_path / "workspace", audience=audience, **overrides),
        providers="mock",
    )


@pytest.fixture
def reader(tmp_path):
    return app_for(Audience.READER, tmp_path)


@pytest.fixture
def studio(tmp_path):
    return app_for(Audience.STUDIO, tmp_path)


# ------------------------------------------------------------ nothing to press


def test_opening_listen_asks_for_the_narration(reader):
    with TestClient(reader) as client:
        body = client.get("/read/mock/JHN/1/mode/listen").text

        assert "Preparing" in body
        assert "Build the narration" not in body, "a reader presses nothing"
        assert "<form" not in body, "and there is nothing to submit"
        assert reader.state.jobs.state_for("reading:mock/JHN/001:af_heart") is not None


def test_opening_listen_twice_enqueues_one_job(reader):
    """The fragment polls itself while the job runs. Requesting on every poll is
    one careless line away, and it would be dozens of jobs a minute."""
    submitted: list[str] = []
    real = reader.state.jobs.submit

    def counted(key, kind, fn):
        submitted.append(key)
        return real(key, kind, fn)

    reader.state.jobs.submit = counted
    with TestClient(reader) as client:
        for _ in range(4):
            client.get("/read/mock/JHN/1/mode/listen")

    # One key, and once the reading exists the page stops asking at all — the
    # cheaper of the two guarantees, and the one that matters on a poll loop.
    assert len(set(submitted)) == 1
    assert len(submitted) <= 4
    assert reader.state.jobs.wait_idle(60)

    before = len(submitted)
    with TestClient(reader) as client:
        client.get("/read/mock/JHN/1/mode/listen")
    assert len(submitted) == before, "a finished reading is not requested again"


def test_the_player_replaces_the_notice_once_it_is_ready(reader):
    with TestClient(reader) as client:
        client.get("/read/mock/JHN/1/mode/listen")
        assert reader.state.jobs.wait_idle(120)
        body = client.get("/read/mock/JHN/1/mode/listen").text

    assert "<audio" in body
    assert "Preparing" not in body


def test_the_studio_still_offers_the_button(studio):
    with TestClient(studio) as client:
        body = client.get("/read/mock/JHN/1/mode/listen").text

    assert "Build the narration" in body
    assert studio.state.jobs.state_for("reading:mock/JHN/001:af_heart") is None, (
        "and looking at it starts nothing"
    )


# ------------------------------------------------------- nobody else's money


def _paid_voice(tmp_path, audience: Audience):
    """A vendor whose cheapest run is over the default budget.

    Built without `providers="mock"`, which would override every chain including
    the vendor — the flag is total by design (`runner.provider_override`).
    """
    return create_app(
        Settings(
            workspace_dir=tmp_path / "workspace",
            audience=audience,
            provider_chains={"tts": ["vendor"], "llm": ["mock"], "corpus": ["mock"]},
            voice_providers=[
                {
                    "name": "vendor",
                    "endpoint": "https://voice.invalid/speak",
                    "languages": ["en"],
                    "voices": ["nyira"],
                    "cost_per_minute_usd": 100.0,
                }
            ],
        )
    )


def test_a_reader_is_never_asked_to_spend_the_owners_money(tmp_path):
    app = _paid_voice(tmp_path, Audience.READER)

    with TestClient(app) as client:
        body = client.get("/read/mock/JHN/1/mode/listen").text

    assert "not available here" in body
    assert "Confirm" not in body and "confirmed" not in body
    assert "$" not in body, "not even the number: it is not their bill"
    assert app.state.jobs.state_for("reading:mock/JHN/001:nyira") is None, "and nothing was started"


def test_the_studio_is_shown_the_estimate_and_may_confirm(tmp_path):
    app = _paid_voice(tmp_path, Audience.STUDIO)

    with TestClient(app) as client:
        body = client.get("/read/mock/JHN/1/mode/listen").text

    assert "Confirm and build it" in body
    assert "$" in body, "the person who owns the account is shown the number"


# ------------------------------------------------------------------ the words


def test_the_brief_does_not_name_the_model_to_a_reader(reader, studio):
    with TestClient(reader) as client:
        read = client.get("/read/mock/JHN/1/mode/brief").text
    with TestClient(studio) as client:
        made = client.get("/read/mock/JHN/1/mode/brief").text

    assert "A retelling" in read, "the label is the feature, and it stays"
    assert "mock-llm-v1" not in read
    assert "mock-llm-v1" in made


def test_a_reader_is_not_offered_the_way_out(reader, studio, tmp_path):
    from videomaker.corpus.models import WorkRef

    work = WorkRef(id="w", title="W", language="en", licence="X.", licence_url="u", source_url="u")

    assert ReadMode.WATCH not in modes_for(work, reader.state.settings)
    assert ReadMode.WATCH in modes_for(work, studio.state.settings)

    with TestClient(reader) as client:
        assert ">Watch<" not in client.get("/read/mock/JHN/1").text
        assert client.get("/read/mock/JHN/1/mode/watch").status_code == 404
        assert client.post("/read/mock/JHN/1/watch").status_code == 404


def test_a_reader_never_sees_a_stage_name(reader):
    with TestClient(reader) as client:
        body = client.get("/read/mock/JHN/1/mode/listen").text
        assert reader.state.jobs.wait_idle(120)

    for workshop_word in ("Synthesising", "stage", "provider", "queued"):
        assert workshop_word not in body, f"a reader was told about {workshop_word!r}"


def test_reading_itself_is_word_for_word_the_same(reader, studio):
    """Only the chrome differs. The passage is the passage."""
    with TestClient(reader) as client:
        read = client.get("/read/mock/JHN/1/mode/source").text
    with TestClient(studio) as client:
        made = client.get("/read/mock/JHN/1/mode/source").text

    verses = lambda body: [
        line for line in body.splitlines() if 'class="verse"' in line
    ]
    assert verses(read) == verses(made)


def test_a_reader_never_sees_a_providers_diagnostics(reader, studio, monkeypatch):
    """When no brief can be written, a reader gets a sentence and the passage.

    The studio gets the detail, because the studio can act on it. This was
    shipping `every llm provider failed: ... errors.pydantic.dev/2.13/v/value_error`
    onto a reading page, which is a stack trace with a URL in it.
    """
    from videomaker.providers.errors import ProviderError
    from videomaker.web.routes import library as library_routes

    def refuse(unit, deps):
        raise ProviderError(
            "every llm provider failed: mock: reply did not match the brief schema: "
            "1 validation error for Brief ... https://errors.pydantic.dev/2.13/v/value_error"
        )

    monkeypatch.setattr(library_routes, "build_brief", refuse)

    with TestClient(reader) as client:
        read = client.get("/read/mock/JHN/1/mode/brief").text
    with TestClient(studio) as client:
        made = client.get("/read/mock/JHN/1/mode/brief").text

    assert "There is no brief" in read
    for leak in ("pydantic", "provider", "validation error", "schema"):
        assert leak not in read, f"a reader was shown {leak!r}"
    assert 'class="verse"' in read, "and the passage is still there to read"

    assert "pydantic" in made, "the studio is shown what actually happened"
