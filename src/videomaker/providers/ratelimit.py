"""Soft free-tier budgets and a persisted sliding-window quota tracker.

Free tiers are policed *before* the call, not after: M0 finding 6 showed
Cloudflare Workers AI silently bills past its free cap rather than failing, so
the tracker has to stop us. Budgets are therefore recorded in provider units
(neurons for Cloudflare, requests everywhere else) and every published limit is
shaded down so a miscount costs headroom, not money.
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

_WINDOWS: tuple[tuple[str, float], ...] = (
    ("rpm", RPM_WINDOW_S),
    ("per_hour", HOUR_WINDOW_S),
    ("per_day", DAY_WINDOW_S),
)

_MAX_WINDOW_S = max(seconds for _, seconds in _WINDOWS)


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f"{path.name}.tmp"
    tmp.write_text(text)
    os.replace(tmp, path)


class QuotaTracker:
    """Counts units spent per provider inside sliding windows, across runs.

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
        cutoff = self._clock() - _MAX_WINDOW_S
        return [(at, units) for at, units in events if at > cutoff]

    def _used(self, provider: str, window_s: float) -> int:
        cutoff = self._clock() - window_s
        return sum(units for at, units in self._events.get(provider, []) if at > cutoff)

    def check(self, provider: str, budget: Budget) -> None:
        """Raise `QuotaExceeded` if the next call would breach the soft budget."""
        for field, window_s in _WINDOWS:
            limit = getattr(budget, field)
            if limit is None:
                continue
            used = self._used(provider, window_s)
            if used >= limit:
                raise QuotaExceeded(
                    f"{provider} soft budget spent: {used}/{limit} units in the last "
                    f"{int(window_s)}s ({field})"
                )

    def record(self, provider: str, units: int = 1) -> None:
        """Book `units` against `provider`. Never raises — accounting is best effort."""
        events = self._events.setdefault(provider, [])
        events.append((self._clock(), units))
        self._events[provider] = self._prune(events)

    def remaining(self, provider: str, budget: Budget) -> dict[str, int | None]:
        """Headroom left on each axis; `None` where the budget is unlimited."""
        headroom: dict[str, int | None] = {}
        for field, window_s in _WINDOWS:
            limit = getattr(budget, field)
            if limit is None:
                headroom[field] = None
            else:
                headroom[field] = max(0, limit - self._used(provider, window_s))
        return headroom

    def save(self) -> None:
        payload = {
            provider: [[at, units] for at, units in self._prune(events)]
            for provider, events in self._events.items()
        }
        _write_atomic(self.path, json.dumps(payload, separators=(",", ":"), sort_keys=True))
