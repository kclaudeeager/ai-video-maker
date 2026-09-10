"""Soft free-tier budgets and a persisted quota tracker.

Free tiers are policed *before* the call, not after: M0 finding 6 showed
Cloudflare Workers AI silently bills past its free cap rather than failing, so
the tracker has to stop us. Budgets are therefore recorded in provider units
(neurons for Cloudflare, requests everywhere else) and every published limit is
shaded down so a miscount costs headroom, not money.

Two shapes of window, because the providers have two shapes of limit:

* `rpm` and `per_hour` **slide** — a rate limit genuinely is a rolling window.
* `per_day` resets on a **UTC calendar boundary**, because a daily cap is not a
  rolling window. Spending Gemini's 240 at 21:00 has to free up at midnight, not
  at 21:00 tomorrow (M1 follow-up 2, M2 follow-up 7).

UTC is a compromise, and it is exactly right for only one of the two providers we
meter daily:

* **Cloudflare** counts neurons on a UTC day. We match it.
* **Gemini**'s free tier resets at midnight *Pacific* — 07:00 UTC under PDT,
  08:00 UTC under PST. One boundary cannot be both.

From Google's reset until the end of the UTC day, our window starts earlier than
theirs, so we count at least what they count and refuse first: the safe
direction. In the seven or eight hours between 00:00 UTC and their reset we are
the permissive one — our counter has cleared and theirs has not, so a call can go
out that Gemini refuses with a daily-cap 429. `providers.llm._raise_for_status`
maps that onto `QuotaExceeded`, so the mismatch costs an error, never money.
Anyone reading a "the dashboard says spent, the tracker says empty" report timed
inside those hours is looking at this decision and not at a bug.
"""

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from videomaker.providers.errors import QuotaExceeded

RPM_WINDOW_S = 60.0
HOUR_WINDOW_S = 3600.0
DAY_WINDOW_S = 86400.0

DEFAULT_QUOTA_PATH = Path.home() / ".cache" / "ai-video-maker" / "quota.json"


@dataclass(frozen=True)
class Budget:
    """A soft allowance; `None` means the provider is unlimited on that axis."""

    rpm: int | None = None
    per_day: int | None = None
    per_hour: int | None = None


# Deliberately under the published hard limits, so a miscount or a concurrent
# process still lands inside the free tier.
SOFT_BUDGETS: dict[str, Budget] = {
    "groq": Budget(rpm=28),  # published 30 rpm
    "gemini": Budget(per_day=240),  # published 250/day
    "pexels": Budget(per_hour=190),  # published 200/hour
    "cloudflare": Budget(per_day=9000),  # published 10k neurons/day
}

#: The axes that slide, and how far back each one reaches. `per_day` is absent
#: on purpose: it is a calendar window, not a rolling one.
_SLIDING_WINDOWS: dict[str, float] = {"rpm": RPM_WINDOW_S, "per_hour": HOUR_WINDOW_S}

#: Every axis of `Budget`, in the order a refusal should consider them.
_AXES: tuple[str, ...] = ("rpm", "per_hour", "per_day")

_MAX_SLIDING_WINDOW_S = max(_SLIDING_WINDOWS.values())


def _utc_day_start(now: float) -> float:
    """The most recent UTC midnight at or before `now`, as a Unix timestamp.

    POSIX time counts non-leap seconds since 1970-01-01T00:00:00Z, so every UTC
    midnight is an exact multiple of 86400 and this is arithmetic, not rounding.
    """
    return now - now % DAY_WINDOW_S


def _window_label(axis: str) -> str:
    """How to describe `axis`'s window to a human reading a refusal."""
    window_s = _SLIDING_WINDOWS.get(axis)
    if window_s is None:
        return "since 00:00 UTC"
    return f"in the last {int(window_s)}s"


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f"{path.name}.tmp"
    tmp.write_text(text)
    os.replace(tmp, path)


class QuotaTracker:
    """Counts units spent per provider inside its budget windows, across runs.

    `check` raises `QuotaExceeded` before a call is made; `record` books what
    that call cost and never raises. Counters survive process restarts once
    `save` has been called.
    """

    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self._clock = clock
        self._events: dict[str, list[tuple[float, int]]] = self._read()

    def _read(self) -> dict[str, list[tuple[float, int]]]:
        try:
            data = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
        if not isinstance(data, dict):
            return {}
        events: dict[str, list[tuple[float, int]]] = {}
        for provider, entries in data.items():
            if not isinstance(entries, list):
                continue
            events[str(provider)] = self._prune(
                [
                    (float(entry[0]), int(entry[1]))
                    for entry in entries
                    if isinstance(entry, list) and len(entry) == 2
                ]
            )
        return events

    def _prune(self, events: list[tuple[float, int]]) -> list[tuple[float, int]]:
        """Drop events that can no longer count against any axis.

        The cutoff is the *earlier* of the two rules. Just after midnight UTC the
        day counter has reset but the hour window still reaches back into
        yesterday, so pruning at the day boundary alone would lose per-hour
        events that are still live.
        """
        now = self._clock()
        cutoff = min(_utc_day_start(now), now - _MAX_SLIDING_WINDOW_S)
        return [(at, units) for at, units in events if at > cutoff]

    def _cutoff(self, axis: str) -> float:
        """The instant before which events stop counting against `axis`."""
        now = self._clock()
        window_s = _SLIDING_WINDOWS.get(axis)
        return now - window_s if window_s is not None else _utc_day_start(now)

    def _used(self, provider: str, axis: str) -> int:
        cutoff = self._cutoff(axis)
        return sum(units for at, units in self._events.get(provider, []) if at > cutoff)

    def check(self, provider: str, budget: Budget) -> None:
        """Raise `QuotaExceeded` if the next call would breach the soft budget."""
        for axis in _AXES:
            limit = getattr(budget, axis)
            if limit is None:
                continue
            used = self._used(provider, axis)
            if used >= limit:
                raise QuotaExceeded(
                    f"{provider} soft budget spent: {used}/{limit} units "
                    f"{_window_label(axis)} ({axis})"
                )

    def record(self, provider: str, units: int = 1) -> None:
        """Book `units` against `provider`. Never raises — accounting is best effort."""
        events = self._events.setdefault(provider, [])
        events.append((self._clock(), units))
        self._events[provider] = self._prune(events)

    def wait_s(self, provider: str, budget: Budget) -> float:
        """Seconds to wait before one more unit fits under every *sliding* axis.

        `check` answers "may I, right now?"; this answers "when?". It exists for
        callers that would rather pace than be refused — a vendor's per-minute cap
        is a client-side sleep, not an event to catch (`providers/tts/http_api.py`).
        `0.0` when the next unit fits now. Calendar axes (`per_day`) are not
        waited on: a reset hours away is a stop, and `day_resets_in_s` says when.
        """
        now = self._clock()
        wait = 0.0
        for axis, window_s in _SLIDING_WINDOWS.items():
            limit = getattr(budget, axis)
            if limit is None:
                continue
            cutoff = now - window_s
            live = sorted(at for at, _units in self._events.get(provider, []) if at > cutoff)
            excess = len(live) - limit + 1
            if excess > 0:
                wait = max(wait, live[excess - 1] + window_s - now)
        return wait

    def since_last_s(self, provider: str) -> float | None:
        """Seconds since `provider` was last booked; `None` if it never was."""
        events = self._events.get(provider)
        if not events:
            return None
        return self._clock() - max(at for at, _units in events)

    def day_resets_in_s(self) -> float:
        """Seconds until the next UTC midnight, when every `per_day` counter clears."""
        now = self._clock()
        return _utc_day_start(now) + DAY_WINDOW_S - now

    def remaining(self, provider: str, budget: Budget) -> dict[str, int | None]:
        """Headroom left on each axis; `None` where the budget is unlimited."""
        headroom: dict[str, int | None] = {}
        for axis in _AXES:
            limit = getattr(budget, axis)
            if limit is None:
                headroom[axis] = None
            else:
                headroom[axis] = max(0, limit - self._used(provider, axis))
        return headroom

    def save(self) -> None:
        payload = {
            provider: [[at, units] for at, units in self._prune(events)]
            for provider, events in self._events.items()
        }
        _write_atomic(self.path, json.dumps(payload, separators=(",", ":"), sort_keys=True))
