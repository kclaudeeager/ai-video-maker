"""What the first-run screen needs: the free-tier headroom, and the keys.

The onboarding screen makes two factual claims — *it costs nothing* and *it needs
these keys* — and a screen that made either of them from a hard-coded string
would be wrong the first time a budget moved. Both are read live:

* the headroom comes from the same `QuotaTracker` ledger and the same
  `SOFT_BUDGETS` the providers are actually policed against, and `remaining` is a
  read that books nothing;
* the keys are reported as set or not set from `Settings`, which is the object
  the providers themselves resolve credentials from.

Deliberately not built on `routes.storyboard.quota_view`: every route module
imports `routes.projects`, so `routes.projects` importing one of them back would
be a cycle. It is also a different question — the onboarding screen asks about
the whole free tier, before any project exists.
"""

from dataclasses import dataclass

from videomaker.config import Settings
from videomaker.providers.ratelimit import DEFAULT_QUOTA_PATH, SOFT_BUDGETS, QuotaTracker

#: How much of a budget may be gone before the row stops reading as "plenty".
_TIGHT = 0.25

#: The provider each budget belongs to, said in terms of what it does for you
#: rather than what it is called. Anything not listed falls back to its own name,
#: so a provider added later still appears.
PROVIDER_ROLES: dict[str, str] = {
    "groq": "writes the script",
    "gemini": "writes the script when Groq is out",
    "pexels": "finds the footage",
    "cloudflare": "draws an image when no footage fits",
}

#: The axis names, as a person would say them.
AXIS_WORDS: dict[str, str] = {
    "rpm": "this minute",
    "per_hour": "this hour",
    "per_day": "today",
}


@dataclass(frozen=True)
class FreeTierRow:
    """One provider's remaining headroom on one axis."""

    provider: str
    role: str
    axis: str
    remaining: int
    limit: int

    @property
    def window(self) -> str:
        return AXIS_WORDS.get(self.axis, self.axis)

    @property
    def tone(self) -> str:
        return "status-ok" if self.remaining > self.limit * _TIGHT else "status-waiting"


@dataclass(frozen=True)
class KeyRow:
    """One credential the tool looks for, and whether it found it."""

    name: str
    env_var: str
    role: str
    present: bool

    @property
    def tone(self) -> str:
        return "status-ok" if self.present else "status-waiting"

    @property
    def state_label(self) -> str:
        return "set" if self.present else "not set"


def free_tier_rows(tracker: QuotaTracker | None = None) -> list[FreeTierRow]:
    """Every soft budget's headroom right now. A read; it books nothing."""
    tracker = tracker if tracker is not None else QuotaTracker(DEFAULT_QUOTA_PATH)
    rows: list[FreeTierRow] = []
    for provider in sorted(SOFT_BUDGETS):
        budget = SOFT_BUDGETS[provider]
        for axis, left in tracker.remaining(provider, budget).items():
            limit = getattr(budget, axis)
            # `None` on an axis is "unlimited there" — no headroom to report.
            if left is None or limit is None:
                continue
            rows.append(
                FreeTierRow(
                    provider=provider,
                    role=PROVIDER_ROLES.get(provider, provider),
                    axis=axis,
                    remaining=left,
                    limit=limit,
                )
            )
    return rows


def key_rows(settings: Settings) -> list[KeyRow]:
    """Which credentials are in place, asked of the same `Settings` providers use."""
    return [
        KeyRow(
            name="Groq",
            env_var="GROQ_API_KEY",
            role=PROVIDER_ROLES["groq"],
            present=bool(settings.groq_api_key),
        ),
        KeyRow(
            name="Google AI Studio",
            env_var="GEMINI_API_KEY",
            role=PROVIDER_ROLES["gemini"],
            present=bool(settings.gemini_api_key),
        ),
        KeyRow(
            name="Pexels",
            env_var="PEXELS_API_KEY",
            role=PROVIDER_ROLES["pexels"],
            present=bool(settings.pexels_api_key),
        ),
        KeyRow(
            name="Cloudflare Workers AI",
            env_var="CLOUDFLARE_API_TOKEN",
            role=PROVIDER_ROLES["cloudflare"],
            present=bool(settings.cloudflare_api_token and settings.cloudflare_account_id),
        ),
    ]
