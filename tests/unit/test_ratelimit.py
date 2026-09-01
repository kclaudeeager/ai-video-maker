import pytest

from videomaker.providers.errors import QuotaExceeded
from videomaker.providers.ratelimit import Budget, QuotaTracker


class FakeClock:
    def __init__(self, now=1_000_000.0): self.now = now
    def __call__(self): return self.now
    def advance(self, seconds): self.now += seconds


#: 2026-01-01T00:00:00Z. An exact UTC midnight (`MIDNIGHT % 86400 == 0`), so a
#: clock set relative to it lands on a known side of the calendar boundary.
MIDNIGHT = 1_767_225_600.0


def test_allows_calls_under_rpm(tmp_path):
    clock = FakeClock()
    q = QuotaTracker(tmp_path / "quota.json", clock=clock)
    budget = Budget(rpm=3, per_day=None, per_hour=None)
    for _ in range(3):
        q.check("groq", budget)
        q.record("groq")


def test_blocks_when_rpm_exceeded(tmp_path):
    clock = FakeClock()
    q = QuotaTracker(tmp_path / "quota.json", clock=clock)
    budget = Budget(rpm=2, per_day=None, per_hour=None)
    for _ in range(2):
        q.check("groq", budget); q.record("groq")
    with pytest.raises(QuotaExceeded):
        q.check("groq", budget)


def test_rpm_window_slides(tmp_path):
    clock = FakeClock()
    q = QuotaTracker(tmp_path / "quota.json", clock=clock)
    budget = Budget(rpm=2, per_day=None, per_hour=None)
    for _ in range(2):
        q.check("groq", budget); q.record("groq")
    clock.advance(61)
    q.check("groq", budget)  # window rolled over


def test_daily_budget_counts_units_not_calls(tmp_path):
    clock = FakeClock()
    q = QuotaTracker(tmp_path / "quota.json", clock=clock)
    budget = Budget(rpm=None, per_day=100, per_hour=None)
    q.check("cloudflare", budget); q.record("cloudflare", units=58)
    q.check("cloudflare", budget); q.record("cloudflare", units=58)
    with pytest.raises(QuotaExceeded):
        q.check("cloudflare", budget)


def test_counters_persist_across_instances(tmp_path):
    path = tmp_path / "quota.json"
    clock = FakeClock()
    budget = Budget(rpm=None, per_day=2, per_hour=None)
    q1 = QuotaTracker(path, clock=clock)
    q1.check("groq", budget); q1.record("groq"); q1.save()
    q2 = QuotaTracker(path, clock=clock)
    q2.check("groq", budget); q2.record("groq"); q2.save()
    q3 = QuotaTracker(path, clock=clock)
    with pytest.raises(QuotaExceeded):
        q3.check("groq", budget)


def test_remaining_reports_headroom(tmp_path):
    q = QuotaTracker(tmp_path / "quota.json", clock=FakeClock())
    budget = Budget(rpm=10, per_day=None, per_hour=None)
    q.record("groq", units=3)
    assert q.remaining("groq", budget)["rpm"] == 7


# ------------------------------------------------------- per_day: calendar, in UTC


def test_per_day_clears_at_the_utc_midnight_boundary(tmp_path):
    """A daily cap is a calendar cap: spending it at 23:00 frees up at 00:00.

    Under the sliding window this fails — only one hour has passed.
    """
    clock = FakeClock(MIDNIGHT - 3600)  # 23:00:00 UTC
    q = QuotaTracker(tmp_path / "quota.json", clock=clock)
    budget = Budget(rpm=None, per_day=2, per_hour=None)
    q.record("gemini")
    q.record("gemini")
    with pytest.raises(QuotaExceeded):
        q.check("gemini", budget)
    clock.advance(3600)  # 00:00:00 UTC, a new calendar day
    assert q.remaining("gemini", budget)["per_day"] == 2
    q.check("gemini", budget)


def test_per_day_does_not_clear_inside_one_utc_day(tmp_path):
    """The counter resets on the boundary and nowhere else."""
    clock = FakeClock(MIDNIGHT + 1)  # 00:00:01 UTC
    q = QuotaTracker(tmp_path / "quota.json", clock=clock)
    budget = Budget(rpm=None, per_day=2, per_hour=None)
    q.record("gemini")
    q.record("gemini")
    clock.advance(86_398)  # 23:59:59 UTC, the same day
    assert q.remaining("gemini", budget)["per_day"] == 0
    with pytest.raises(QuotaExceeded):
        q.check("gemini", budget)


def test_per_day_refusal_names_the_utc_day(tmp_path):
    """The message has to say *which* day, or the dashboard comparison is guesswork."""
    q = QuotaTracker(tmp_path / "quota.json", clock=FakeClock(MIDNIGHT + 3600))
    budget = Budget(rpm=None, per_day=1, per_hour=None)
    q.record("gemini")
    with pytest.raises(QuotaExceeded) as excinfo:
        q.check("gemini", budget)
    assert "UTC" in str(excinfo.value)


def test_yesterdays_bookings_are_gone_after_a_reload(tmp_path):
    """The calendar rule survives the ledger round-trip, not just the live object."""
    path = tmp_path / "quota.json"
    clock = FakeClock(MIDNIGHT - 60)  # 23:59:00 UTC
    budget = Budget(rpm=None, per_day=2, per_hour=None)
    q1 = QuotaTracker(path, clock=clock)
    q1.record("gemini")
    q1.record("gemini")
    q1.save()
    clock.advance(120)  # 00:01:00 UTC, the next day
    q2 = QuotaTracker(path, clock=clock)
    assert q2.remaining("gemini", budget)["per_day"] == 2
    q2.check("gemini", budget)
    q2.record("gemini")
    q2.save()
    assert QuotaTracker(path, clock=clock).remaining("gemini", budget)["per_day"] == 1


def test_a_days_bookings_outlive_the_hour_window_in_the_ledger(tmp_path):
    """Pruning to the sliding hour would quietly reset the *daily* counter hourly.

    `_prune` runs on every `record`, `save` and load, so a retention rule shorter
    than the calendar day would drop this morning's bookings by lunchtime.
    """
    path = tmp_path / "quota.json"
    clock = FakeClock(MIDNIGHT + 600)  # 00:10:00 UTC
    budget = Budget(rpm=None, per_day=2, per_hour=None)
    q1 = QuotaTracker(path, clock=clock)
    q1.record("gemini")
    q1.record("gemini")
    q1.save()
    clock.advance(6 * 3600)  # 06:10:00 UTC, the same day, six hours on
    q2 = QuotaTracker(path, clock=clock)
    assert q2.remaining("gemini", budget)["per_day"] == 0
    q2.record("gemini")
    q2.save()
    with pytest.raises(QuotaExceeded):
        QuotaTracker(path, clock=clock).check("gemini", budget)


def test_hour_window_reaches_back_across_the_day_boundary(tmp_path):
    """Pruning to the day start alone would silently forget an in-window hour event."""
    path = tmp_path / "quota.json"
    clock = FakeClock(MIDNIGHT - 600)  # 23:50:00 UTC
    q1 = QuotaTracker(path, clock=clock)
    q1.record("pexels", units=5)
    q1.save()
    clock.advance(1200)  # 00:10:00 UTC — 20 minutes later, but a new day
    q2 = QuotaTracker(path, clock=clock)
    budget = Budget(rpm=None, per_day=None, per_hour=10)
    assert q2.remaining("pexels", budget)["per_hour"] == 5


# ---------------------------------------------- rpm and per_hour stay *sliding*


def test_rpm_does_not_reset_on_a_clock_boundary(tmp_path):
    """A per-minute cap resetting on the minute would let a burst through at :59/:00."""
    clock = FakeClock(MIDNIGHT - 1)  # 23:59:59 UTC — one second before minute,
    q = QuotaTracker(tmp_path / "quota.json", clock=clock)  # hour and day all roll
    budget = Budget(rpm=2, per_day=None, per_hour=None)
    q.record("groq")
    q.record("groq")
    clock.advance(2)  # 00:00:01 UTC: three boundaries crossed, two seconds spent
    with pytest.raises(QuotaExceeded):
        q.check("groq", budget)


def test_per_hour_does_not_reset_on_a_clock_boundary(tmp_path):
    """Same for the hourly axis: Pexels' 200/hour is a rolling limit, not a calendar one."""
    clock = FakeClock(MIDNIGHT - 1)  # 23:59:59 UTC
    q = QuotaTracker(tmp_path / "quota.json", clock=clock)
    budget = Budget(rpm=None, per_day=None, per_hour=2)
    q.record("pexels")
    q.record("pexels")
    clock.advance(2)  # 00:00:01 UTC
    with pytest.raises(QuotaExceeded) as excinfo:
        q.check("pexels", budget)
    # The wording the onboarding card renders (`test_web_stock_search`) is this one.
    assert str(excinfo.value).endswith("units in the last 3600s (per_hour)")
