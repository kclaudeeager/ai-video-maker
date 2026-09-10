"""What a visitor may spend, and what costs nothing.

A brief is written on `GET` of the brief mode, so on a reading server it is the
one thing a stranger can spend: a crawler walking 1,189 chapters of a Bible is
1,189 LLM calls. `SOFT_BUDGETS` already stops that becoming a *bill* — the free
tier refuses past its cap — but it does not stop one visitor spending the owner's
whole day before the owner gets to it. This is the budget that does.

The property that makes it usable rather than merely safe is
`test_a_cached_brief_is_free_however_often_it_is_read`: the count is of **new**
briefs, so a popular chapter costs one call ever, and somebody re-opening a page
they have already seen can never be refused.
"""

import pytest
from fastapi.testclient import TestClient

from videomaker.config import Audience, Settings, load_settings
from videomaker.corpus.audio import reader_deps
from videomaker.corpus.digest import (
    READER_BRIEF_KEY,
    build_brief_within_budget,
    reader_budget,
)
from videomaker.corpus.models import UnitRef, UnitText, Verse
from videomaker.providers.errors import QuotaExceeded
from videomaker.providers.ratelimit import QuotaTracker
from videomaker.runner import PROVIDER_KINDS
from videomaker.web.app import create_app


class Clock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


def settings_for(tmp_path, **overrides) -> Settings:
    return Settings(
        workspace_dir=tmp_path / "workspace",
        provider_chains={kind: ["mock"] for kind in PROVIDER_KINDS},
        audience=Audience.READER,
        **overrides,
    )


@pytest.fixture
def deps(tmp_path, clock):
    settings = settings_for(tmp_path, reader_briefs_per_day=3, reader_briefs_per_minute=2)
    built = reader_deps(settings, cache_dir=tmp_path / "cache")
    built.quota = QuotaTracker(tmp_path / "cache" / "quota.json", clock=clock)
    return built


def unit_for(deps, chapter: int):
    return deps.provider("corpus").unit(UnitRef(work_id="mock", book="JHN", chapter=chapter))


def fresh_unit(n: int) -> UnitText:
    """A passage nothing has briefed yet.

    Distinct *text*, not a distinct title: `brief_key` hashes `unit.plain`, so a
    retitled passage is the same brief and is rightly served from cache.
    """
    return UnitText(
        ref=UnitRef(work_id="mock", book="JHN", chapter=1),
        title=f"Made up {n}",
        verses=[Verse(number=1, text=f"A sentence nobody has summarised before, number {n}.")],
    )


def spent(deps) -> int:
    left = deps.quota.remaining(READER_BRIEF_KEY, reader_budget(deps.settings))
    return deps.settings.reader_briefs_per_day - (left["per_day"] or 0)


# ------------------------------------------------------------- what costs what


def test_a_new_brief_is_counted_once(deps):
    build_brief_within_budget(unit_for(deps, 1), deps)

    assert spent(deps) == 1


def test_a_cached_brief_is_free_however_often_it_is_read(deps, clock):
    unit = unit_for(deps, 1)
    build_brief_within_budget(unit, deps)
    assert spent(deps) == 1

    for _ in range(20):
        clock.now += 1
        build_brief_within_budget(unit, deps)

    assert spent(deps) == 1, "the cache is what makes a popular chapter cheap"


def test_a_reader_can_always_re_open_what_they_have_already_seen(deps, clock):
    """Even with the day spent: a cached brief is never checked against a budget."""
    unit = unit_for(deps, 1)
    build_brief_within_budget(unit, deps)
    deps.quota.record(READER_BRIEF_KEY, deps.settings.reader_briefs_per_day)

    clock.now += 120
    assert build_brief_within_budget(unit, deps).summary


# ------------------------------------------------------------------- the caps


def test_the_day_is_capped(deps, clock):
    for chapter in (1, 2, 3):
        clock.now += 60  # past the per-minute cap each time
        build_brief_within_budget(unit_for(deps, chapter), deps)

    clock.now += 60
    with pytest.raises(QuotaExceeded, match="per_day"):
        build_brief_within_budget(fresh_unit(99), deps)


def test_a_burst_meets_the_minute_cap_first(deps):
    """What a crawler walking a whole Bible hits before it hits the daily one."""
    build_brief_within_budget(unit_for(deps, 1), deps)
    build_brief_within_budget(unit_for(deps, 2), deps)

    with pytest.raises(QuotaExceeded, match="rpm"):
        build_brief_within_budget(unit_for(deps, 3), deps)


def test_the_minute_cap_lets_go(deps, clock):
    build_brief_within_budget(unit_for(deps, 1), deps)
    build_brief_within_budget(unit_for(deps, 2), deps)

    clock.now += 61
    assert build_brief_within_budget(unit_for(deps, 3), deps).summary


def test_the_count_survives_a_restart(deps, clock, tmp_path):
    build_brief_within_budget(unit_for(deps, 1), deps)

    again = QuotaTracker(tmp_path / "cache" / "quota.json", clock=clock)
    left = again.remaining(READER_BRIEF_KEY, reader_budget(deps.settings))

    assert left["per_day"] == deps.settings.reader_briefs_per_day - 1


def test_zero_means_unlimited_on_that_axis(tmp_path):
    budget = reader_budget(settings_for(tmp_path, reader_briefs_per_day=0))

    assert budget.per_day is None
    assert budget.rpm is not None


def test_the_caps_are_well_under_the_provider_budget():
    """A visitor must not be able to spend the owner's whole free tier."""
    from videomaker.providers.ratelimit import SOFT_BUDGETS

    assert Settings().reader_briefs_per_day < SOFT_BUDGETS["gemini"].per_day


def test_the_caps_can_be_configured(tmp_path):
    (tmp_path / "config.yaml").write_text("reader_briefs_per_day: 7\nreader_briefs_per_minute: 1\n")
    settings = load_settings(tmp_path / "config.yaml")

    assert (settings.reader_briefs_per_day, settings.reader_briefs_per_minute) == (7, 1)


# ------------------------------------------------------- only for the visitor


@pytest.mark.parametrize(
    ("audience", "booked"),
    [(Audience.STUDIO, 0), (Audience.READER, 3)],
)
def test_only_a_visitors_briefs_are_booked(tmp_path, monkeypatch, audience, booked):
    """The owner spends their own quota deliberately; the provider budget is
    their limit, and a visitor budget applied to them would be a bug.

    Read from the ledger the app actually writes — `reader_deps` builds its
    tracker under `runner.USER_CACHE_DIR` — rather than from a fresh file that
    could only ever be empty.
    """
    from videomaker import runner as runner_module

    ledger = tmp_path / "cache"
    monkeypatch.setattr(runner_module, "USER_CACHE_DIR", ledger)
    app = create_app(
        Settings(
            workspace_dir=tmp_path / "workspace",
            audience=audience,
            reader_briefs_per_day=50,
            reader_briefs_per_minute=50,
        ),
        providers="mock",
    )

    with TestClient(app) as client:
        for chapter in (1, 2, 3):
            assert client.get(f"/read/mock/JHN/{chapter}/mode/brief").status_code == 200

    tracker = QuotaTracker(ledger / "quota.json")
    left = tracker.remaining(READER_BRIEF_KEY, reader_budget(app.state.settings))
    assert 50 - (left["per_day"] or 0) == booked


def test_over_budget_reads_as_no_brief_yet_rather_than_an_error(tmp_path, monkeypatch):
    """Over budget is a quieter day, not a failure: the passage is still there."""
    from videomaker.web.routes import library as library_routes

    def spent_out(unit, deps):
        raise QuotaExceeded("reader:brief soft budget spent: 50/50 units since 00:00 UTC (per_day)")

    monkeypatch.setattr(library_routes, "build_brief_within_budget", spent_out)
    reader = create_app(
        Settings(workspace_dir=tmp_path / "workspace", audience=Audience.READER), providers="mock"
    )

    with TestClient(reader) as client:
        response = client.get("/read/mock/JHN/1/mode/brief")

    assert response.status_code == 200, "not an error page"
    assert "There is no brief" in response.text
    assert 'class="verse"' in response.text, "and the passage is right there"
    assert "budget" not in response.text and "quota" not in response.text


# ---------------------------------------------------------------- and doctor


def test_doctor_reports_the_headroom_on_a_reading_server(tmp_path, monkeypatch):
    from videomaker import runner as runner_module
    from videomaker.doctor import _check_reader_budget

    # Its own ledger. `conftest` redirects `USER_CACHE_DIR` once for the whole
    # session, so any earlier test that read a brief as a visitor has already
    # spent from the shared one — which is correct behaviour and made this test
    # order-dependent until it stopped reading global state.
    monkeypatch.setattr(runner_module, "USER_CACHE_DIR", tmp_path / "own-cache")

    row = _check_reader_budget(settings_for(tmp_path, reader_briefs_per_day=9))

    assert row.level == "ok"
    assert "9 of 9 briefs left today" in row.detail


def test_doctor_says_nothing_about_it_in_a_studio(tmp_path):
    from videomaker.doctor import _check_reader_budget

    row = _check_reader_budget(
        Settings(workspace_dir=tmp_path, audience=Audience.STUDIO)
    )

    assert row.level == "ok"
    assert "no visitor budget" in row.detail


def test_a_spent_day_is_a_warning_naming_the_reset(tmp_path, monkeypatch):
    from videomaker import runner as runner_module
    from videomaker.doctor import _check_reader_budget

    monkeypatch.setattr(runner_module, "USER_CACHE_DIR", tmp_path)
    tracker = QuotaTracker(tmp_path / "quota.json")
    tracker.record(READER_BRIEF_KEY, 9)
    tracker.save()

    row = _check_reader_budget(settings_for(tmp_path, reader_briefs_per_day=9))

    assert row.level == "warn"
    assert "00:00 UTC" in row.detail
    assert row.fix
