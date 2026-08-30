import pytest

from videomaker.providers.errors import QuotaExceeded
from videomaker.providers.ratelimit import Budget, QuotaTracker


class FakeClock:
    def __init__(self): self.now = 1_000_000.0
    def __call__(self): return self.now
    def advance(self, seconds): self.now += seconds


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
