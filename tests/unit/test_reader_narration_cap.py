"""What a visitor may make the machine synthesise.

`TTSBudget` guards money and this guards CPU. Neither substitutes for the other:
a local Kokoro voice costs nothing and runs at about realtime, so a chapter of 51
verses is six minutes of work, and a visitor walking a Bible on a reading server
queues that against one worker until the machine grinds.

The unit is **minutes of audio, not requests**. Psalm 119 is 176 verses and 2
John is 13; counting both as "one request" would make the cap meaningless at one
end and cruel at the other.
"""

import pytest
from fastapi.testclient import TestClient

from videomaker.config import Audience, Settings
from videomaker.corpus.audio import (
    READER_NARRATION_KEY,
    narration_budget,
    narration_minutes,
    reader_deps,
)
from videomaker.corpus.models import UnitRef, UnitText, Verse
from videomaker.providers.ratelimit import QuotaTracker
from videomaker.web.app import create_app


def app_for(audience: Audience, tmp_path, **overrides):
    return create_app(
        Settings(workspace_dir=tmp_path / "workspace", audience=audience, **overrides),
        providers="mock",
    )


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """Its own quota file: `conftest` shares one for the whole session."""
    from videomaker import runner as runner_module

    monkeypatch.setattr(runner_module, "USER_CACHE_DIR", tmp_path / "cache")
    return tmp_path / "cache" / "quota.json"


def spent(ledger, settings) -> int:
    left = QuotaTracker(ledger).remaining(READER_NARRATION_KEY, narration_budget(settings))
    return settings.reader_narration_minutes_per_day - (left["per_day"] or 0)


# --------------------------------------------------------------------- the unit


def test_a_long_passage_costs_more_than_a_short_one():
    short = UnitText(
        ref=UnitRef(work_id="w", book="JHN", chapter=1),
        title="Short",
        verses=[Verse(number=1, text="Two words.")],
    )
    long = UnitText(
        ref=short.ref,
        title="Long",
        verses=[Verse(number=n, text=" ".join(["word"] * 150)) for n in range(1, 11)],
    )

    assert narration_minutes(short) == 1, "and nothing is free"
    assert narration_minutes(long) == 10
    assert narration_minutes(long) > narration_minutes(short)


def test_speed_is_priced_in():
    unit = UnitText(
        ref=UnitRef(work_id="w", book="JHN", chapter=1),
        title="X",
        verses=[Verse(number=1, text=" ".join(["word"] * 600))],
    )

    assert narration_minutes(unit, speed=1.0) == 4
    assert narration_minutes(unit, speed=2.0) == 2


def test_zero_means_unlimited_on_that_axis(tmp_path):
    budget = narration_budget(
        Settings(workspace_dir=tmp_path, reader_narration_minutes_per_day=0)
    )

    assert budget.per_day is None
    assert budget.rpm is not None


# ------------------------------------------------------------------ what costs


def test_a_visitors_request_is_counted_in_minutes(tmp_path, ledger):
    app = app_for(Audience.READER, tmp_path)

    with TestClient(app) as client:
        client.get("/read/mock/JHN/1/mode/listen")
        assert app.state.jobs.wait_idle(120)

    assert spent(ledger, app.state.settings) >= 1


def test_a_reading_already_made_is_free(tmp_path, ledger):
    app = app_for(Audience.READER, tmp_path)

    with TestClient(app) as client:
        client.get("/read/mock/JHN/1/mode/listen")
        assert app.state.jobs.wait_idle(120)
        first = spent(ledger, app.state.settings)

        for _ in range(5):
            client.get("/read/mock/JHN/1/mode/listen")

    assert spent(ledger, app.state.settings) == first, "re-hearing costs nothing"


def test_a_request_in_flight_is_counted_once(tmp_path, ledger):
    """The panel polls itself; a poll that booked minutes would charge a visitor
    once a second for waiting."""
    app = app_for(Audience.READER, tmp_path)
    app.state.jobs.stop()  # nothing drains, so the job stays queued

    with TestClient(app) as client:
        for _ in range(4):
            client.get("/read/mock/JHN/1/mode/listen")

    assert spent(ledger, app.state.settings) == narration_minutes(
        _unit_of(app, 1)
    ), "one request, one charge"


def test_posting_the_same_request_repeatedly_is_charged_once(tmp_path, ledger):
    """The POST route calls the gate directly, without the panel's own poll check,
    so this is the path where charging twice for one job is actually reachable."""
    app = app_for(Audience.READER, tmp_path, reader_narration_minutes_per_day=1000)
    app.state.jobs.stop()  # nothing drains, so the job stays queued

    with TestClient(app) as client:
        for _ in range(4):
            assert client.post("/read/mock/JHN/1/audio").status_code == 200

    assert spent(ledger, app.state.settings) == narration_minutes(_unit_of(app, 1))


def _unit_of(app, chapter: int) -> UnitText:
    return reader_deps(app.state.settings).provider("corpus").unit(
        UnitRef(work_id="mock", book="JHN", chapter=chapter)
    )


# -------------------------------------------------------------------- the caps


def test_the_day_refuses_the_next_request_and_enqueues_nothing(tmp_path, ledger):
    app = app_for(
        Audience.READER, tmp_path, reader_narration_minutes_per_day=1, reader_narration_burst_minutes=0
    )
    tracker = QuotaTracker(ledger)
    tracker.record(READER_NARRATION_KEY, 1)
    tracker.save()

    with TestClient(app) as client:
        body = client.get("/read/mock/JHN/1/mode/listen").text

    assert "not ready just now" in body
    assert "Preparing" not in body
    assert app.state.jobs.state_for("reading:mock/JHN/001:af_heart") is None


def test_the_burst_refuses_a_rapid_second_chapter(tmp_path, ledger):
    app = app_for(
        Audience.READER, tmp_path, reader_narration_minutes_per_day=1000, reader_narration_burst_minutes=1
    )

    with TestClient(app) as client:
        first = client.get("/read/mock/JHN/1/mode/listen").text
        second = client.get("/read/mock/JHN/2/mode/listen").text

    assert "Preparing" in first
    assert "not ready just now" in second
    assert app.state.jobs.state_for("reading:mock/JHN/002:af_heart") is None


def test_the_post_route_is_capped_too(tmp_path, ledger):
    """A reading server routes it even though its template offers no button. A
    guard on the path with a button is not a guard."""
    app = app_for(
        Audience.READER, tmp_path, reader_narration_minutes_per_day=1000, reader_narration_burst_minutes=1
    )

    with TestClient(app) as client:
        client.get("/read/mock/JHN/1/mode/listen")
        posted = client.post("/read/mock/JHN/2/audio")

    assert posted.status_code == 200
    assert "not ready just now" in posted.text
    assert app.state.jobs.state_for("reading:mock/JHN/002:af_heart") is None


def test_over_capacity_still_reads(tmp_path, ledger):
    app = app_for(Audience.READER, tmp_path, reader_narration_minutes_per_day=0, reader_narration_burst_minutes=1)
    tracker = QuotaTracker(ledger)
    tracker.record(READER_NARRATION_KEY, 5)
    tracker.save()

    with TestClient(app) as client:
        body = client.get("/read/mock/JHN/1/mode/listen").text

    assert 'class="verse"' in body, "the passage is right there"
    assert "budget" not in body and "quota" not in body and "allowance" not in body


# ------------------------------------------------------------ not in a studio


def test_a_studio_is_capped_by_neither(tmp_path, ledger):
    app = app_for(
        Audience.STUDIO, tmp_path, reader_narration_minutes_per_day=1, reader_narration_burst_minutes=1
    )

    with TestClient(app) as client:
        for chapter in (1, 2, 3):
            assert client.post(f"/read/mock/JHN/{chapter}/audio").status_code == 200
        assert app.state.jobs.wait_idle(180)

    assert spent(ledger, app.state.settings) == 0, "the owner's machine is theirs to grind"


# ---------------------------------------------------------------------- doctor


def test_doctor_reports_both_headrooms(tmp_path, ledger):
    from videomaker.doctor import _check_reader_budget

    row = _check_reader_budget(
        Settings(workspace_dir=tmp_path, audience=Audience.READER, reader_narration_minutes_per_day=42)
    )

    assert "42 of 42 narration minutes left today" in row.detail
    assert "briefs" in row.detail


def test_doctor_warns_when_the_narration_day_is_gone(tmp_path, ledger):
    from videomaker.doctor import _check_reader_budget

    tracker = QuotaTracker(ledger)
    tracker.record(READER_NARRATION_KEY, 42)
    tracker.save()

    row = _check_reader_budget(
        Settings(workspace_dir=tmp_path, audience=Audience.READER, reader_narration_minutes_per_day=42)
    )

    assert row.level == "warn"
    assert "narration" in row.detail
    assert "00:00 UTC" in row.detail
