"""A vendor-neutral HTTP voice provider, with a budget guard and a rate limiter.

**Why this exists.** Kokoro covers the five measured languages of
`docs/language-support.md` and nothing else, and it phonemises through espeak-ng,
which ships Swahili and not Kinyarwanda — so no amount of local work reaches
Kinyarwanda by that path (design doc §9.1). A vendor's HTTP API does. This module
is what keeps that a *configuration* choice: a vendor is a `voice_providers:` entry
in `config.yaml` naming an endpoint, a key's environment variable and a handful of
field names, and **no vendor name appears in Python anywhere**.

Two guards, and they are separate concerns because they are separate failures:

* **The budget stops you spending too much.** A whole Bible is ~5,270 narration
  minutes; at $0.10/minute that is ~$527 in one command. A tool that can do that
  silently is a tool that will, once. So a paid run is estimated first and refused
  past `TTSBudget` unless it was confirmed — by `--yes` or by a button that showed
  the number.
* **The rate limiter stops you being cut off mid-chapter.** A chapter is dozens of
  short requests back to back, exactly the shape a per-minute cap rejects. So the
  per-minute cap is paced client-side by sleeping — being rate-limited is a bug in
  the caller, not an event to handle — and the per-day cap lives in the shared
  `QuotaTracker` ledger, so it survives a restart and a run that would cross it
  stops with the count and the reset time rather than failing at verse 300.

`429` and `5xx` are retried with exponential backoff and full jitter, honouring
`Retry-After`; any other `4xx` is not — a malformed request retried four times is
four times wrong. The API key is read from the environment and never written into
a log line or an exception: `_redact` is applied to every message this module
raises, and a test asserts the key is absent from them.
"""

import base64
import os
import random
import time
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from videomaker.config import Settings
from videomaker.media.ffmpeg import probe_json
from videomaker.providers.base import TTSProvider, TTSResult
from videomaker.providers.errors import ProviderConfigError, ProviderResponseError
from videomaker.providers.llm import parse_retry_after
from videomaker.providers.ratelimit import DEFAULT_QUOTA_PATH, Budget, QuotaTracker

#: Silent narration reads at about 150 words a minute — the figure the voice stage
#: was measured against (Kokoro's `af_heart` at speed 1.0 lands at 145–160 on the
#: M1 scripts), so an estimate made from it is within a tenth of the bill.
DEFAULT_WPM = 150

#: Where retries stop growing. Four attempts at base 1 s is at most ~15 s of
#: waiting; a vendor that has not recovered by then is a `RateLimited` for the
#: caller to resume from, not a longer sleep.
MAX_BACKOFF_S = 30.0

#: Injected here rather than called directly so a test can assert a sleep
#: happened without waiting for it.
sleep = time.sleep
_random = random.random

RETRY_STATUSES = frozenset({429, 408})


class HTTPTTSConfig(BaseModel):
    name: str
    endpoint: str
    api_key_env: str = ""
    auth_header: str = "Authorization"
    auth_format: str = "Bearer {key}"
    text_field: str = "text"
    voice_field: str = "voice"
    language_field: str = "language"
    speed_field: str = ""
    extra_body: dict[str, str] = Field(default_factory=dict)
    audio_response: Literal["raw", "base64_json"] = "raw"
    audio_json_path: str = ""
    voices: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    cost_per_minute_usd: float = 0.0
    timeout_s: float = 60.0
    # Rate limiting. Zero means "no stated limit" and applies no throttle.
    requests_per_minute: int = 0
    requests_per_day: int = 0
    max_concurrency: int = 1
    retry_attempts: int = 4
    retry_base_delay_s: float = 1.0


class TTSBudget(BaseModel):
    max_minutes_per_run: float = 30.0
    max_usd_per_run: float = 1.00
    confirmed: bool = False


class TTSBudgetExceeded(RuntimeError):
    """A paid run would cost more than the budget allows and nobody confirmed it."""


class RateLimited(RuntimeError):
    """The vendor said slow down, and the retries were exhausted — or the daily
    cap would be crossed. Resume later; every verse already made is cached."""

    def __init__(self, message: str, *, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


def estimate_minutes(text: str, *, wpm: int = DEFAULT_WPM) -> float:
    return len(text.split()) / wpm


def check_budget(minutes: float, *, cfg: HTTPTTSConfig, budget: TTSBudget) -> None:
    """Raise `TTSBudgetExceeded` unless `minutes` fits the budget or was confirmed.

    A local provider — `cost_per_minute_usd == 0.0` — is never blocked by the USD
    limit, because its cost is zero and zero exceeds nothing; there is no special
    case to keep in step. The minutes limit still applies, because thirty minutes
    of anything is worth a question.
    """
    if budget.confirmed:
        return
    usd = minutes * cfg.cost_per_minute_usd
    if minutes > budget.max_minutes_per_run:
        raise TTSBudgetExceeded(
            f"{cfg.name}: this run is about {minutes:.1f} minutes of narration, over the "
            f"{budget.max_minutes_per_run:g}-minute limit. Confirm it (--yes, or the "
            "confirm button) to go ahead."
        )
    if usd > budget.max_usd_per_run:
        raise TTSBudgetExceeded(
            f"{cfg.name}: this run is about {minutes:.1f} minutes at "
            f"${cfg.cost_per_minute_usd:.3f}/min, roughly ${usd:.2f}, over the "
            f"${budget.max_usd_per_run:.2f} limit. Confirm it (--yes, or the confirm "
            "button) to go ahead."
        )


def throttle(cfg: HTTPTTSConfig, tracker: QuotaTracker) -> None:
    """Pace before sending: sleep under the per-minute cap, stop at the daily one."""
    if cfg.requests_per_day > 0:
        used = cfg.requests_per_day - (
            tracker.remaining(cfg.name, Budget(per_day=cfg.requests_per_day))["per_day"] or 0
        )
        if used >= cfg.requests_per_day:
            resets = tracker.day_resets_in_s()
            raise RateLimited(
                f"{cfg.name}: {used}/{cfg.requests_per_day} requests used today; the cap "
                f"resets in {resets / 3600:.1f} h (00:00 UTC). Run again then — every verse "
                "already synthesised is kept.",
                retry_after_s=resets,
            )
    if cfg.requests_per_minute > 0:
        # Even spacing, not a sliding-window burst: 60/min is one a second, every
        # second. A burst of sixty in the first second is inside a rolling window
        # the vendor may not be counting the way we are; spacing is inside every
        # window anyone could be counting. `wait_s` still stands behind it for a
        # ledger that already holds a burst from another process.
        interval = 60.0 / cfg.requests_per_minute
        since = tracker.since_last_s(cfg.name)
        spacing = 0.0 if since is None else interval - since
        wait = max(spacing, tracker.wait_s(cfg.name, Budget(rpm=cfg.requests_per_minute)))
        if wait > 0:
            sleep(wait)


def _walk(payload: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        if isinstance(payload, list) and part.isdigit():
            payload = payload[int(part)]
        elif isinstance(payload, dict):
            payload = payload[part]
        else:
            raise KeyError(part)
    return payload


class HTTPTTSProvider(TTSProvider):
    """`POST` one verse, get audio back — over any vendor `HTTPTTSConfig` can describe.

    `client` and `quota` are attributes rather than constructor arguments because
    the signature is the plan's, and because that is how the rest of the project
    injects them: `StageDeps._instance` sets `quota` on any provider that has the
    attribute, and tests set `client` to an `httpx.MockTransport`.
    """

    def __init__(self, cfg: HTTPTTSConfig, *, budget: TTSBudget | None = None) -> None:
        self.cfg = cfg
        self.budget = budget if budget is not None else TTSBudget()
        self.quota: QuotaTracker | None = None
        self._client: httpx.Client | None = None
        #: Minutes synthesised by this instance, so a run that is fine verse by
        #: verse still trips the guard when its verses add up.
        self.minutes_this_run = 0.0

    # ------------------------------------------------------------- plumbing

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.cfg.timeout_s)
        return self._client

    @client.setter
    def client(self, client: httpx.Client) -> None:
        self._client = client

    @property
    def tracker(self) -> QuotaTracker:
        if self.quota is None:
            self.quota = QuotaTracker(DEFAULT_QUOTA_PATH)
        return self.quota

    def _key(self) -> str:
        if not self.cfg.api_key_env:
            return ""
        key = os.environ.get(self.cfg.api_key_env, "")
        if not key:
            raise ProviderConfigError(
                f"{self.cfg.name}: the API key is not set; export {self.cfg.api_key_env} "
                "or put it in .env"
            )
        return key

    def _redact(self, text: str) -> str:
        """The one place the key could leak into a message, closed."""
        key = os.environ.get(self.cfg.api_key_env, "") if self.cfg.api_key_env else ""
        return text.replace(key, "***") if key else text

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        key = self._key()
        if key:
            headers[self.cfg.auth_header] = self.cfg.auth_format.format(key=key)
        return headers

    def _body(self, *, text: str, voice: str, language: str, speed: float) -> dict[str, Any]:
        body: dict[str, Any] = {
            self.cfg.text_field: text,
            self.cfg.voice_field: voice,
            self.cfg.language_field: language,
        }
        if self.cfg.speed_field:
            body[self.cfg.speed_field] = speed
        body.update(self.cfg.extra_body)
        return body

    # ------------------------------------------------------------- contract

    def voices(self) -> list[str]:
        return list(self.cfg.voices)

    def reserve(self, requests: int) -> None:
        """Refuse a run of `requests` calls that would cross the daily cap — before
        the first one, with the count and the reset time, rather than at verse 300."""
        if self.cfg.requests_per_day <= 0 or requests <= 0:
            return
        left = self.tracker.remaining(self.cfg.name, Budget(per_day=self.cfg.requests_per_day))
        remaining = left["per_day"] or 0
        if requests > remaining:
            resets = self.tracker.day_resets_in_s()
            raise RateLimited(
                f"{self.cfg.name}: this run needs {requests} requests and "
                f"{remaining}/{self.cfg.requests_per_day} are left today; the cap resets in "
                f"{resets / 3600:.1f} h (00:00 UTC). Every verse already synthesised is kept.",
                retry_after_s=resets,
            )

    def synthesize(
        self,
        *,
        text: str,
        voice: str,
        out_path: Path,
        speed: float = 1.0,
        language: str = "en",
    ) -> TTSResult:
        minutes = estimate_minutes(text) / max(speed, 0.01)
        check_budget(self.minutes_this_run + minutes, cfg=self.cfg, budget=self.budget)
        headers = self._headers()
        body = self._body(text=text, voice=voice, language=language, speed=speed)
        audio = self._post_with_retry(headers, body)
        path = Path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.part")
        tmp.write_bytes(audio)
        os.replace(tmp, path)
        self.minutes_this_run += minutes
        info = probe_json(path)
        duration_s = float(info.get("format", {}).get("duration") or 0.0)
        streams = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
        sample_rate = int(streams[0].get("sample_rate", 0)) if streams else 0
        return TTSResult(path=path, duration_s=duration_s, sample_rate=sample_rate)

    # --------------------------------------------------------------- the wire

    def _post_with_retry(self, headers: dict[str, str], body: dict[str, Any]) -> bytes:
        cfg = self.cfg
        last: str = ""
        retry_after: float | None = None
        for attempt in range(max(cfg.retry_attempts, 1)):
            throttle(cfg, self.tracker)
            try:
                response = self.client.post(cfg.endpoint, headers=headers, json=body)
            except httpx.TransportError as exc:
                # A timeout or a dropped connection: the same backoff as a 5xx, not
                # a longer timeout — a slow vendor is a vendor to back off from.
                last = f"no reply within {cfg.timeout_s:g}s ({exc.__class__.__name__})"
                response = None
            self.tracker.record(cfg.name)
            self.tracker.save()
            if response is not None:
                if response.is_success:
                    return self._audio_from(response)
                status = response.status_code
                if status not in RETRY_STATUSES and status < 500:
                    raise ProviderResponseError(
                        self._redact(f"{cfg.name} returned {status}: {response.text[:200]}")
                    )
                retry_after = parse_retry_after(response.headers.get("Retry-After"))
                last = self._redact(f"{status}: {response.text[:120]}")
            delay = retry_after
            if delay is None:
                # Full jitter: uniform on [0, base * 2^attempt], capped. Spreads
                # retries from many verses so they do not re-collide on the cap.
                delay = _random() * min(cfg.retry_base_delay_s * (2**attempt), MAX_BACKOFF_S)
            sleep(delay)
        raise RateLimited(
            f"{cfg.name}: gave up after {cfg.retry_attempts} attempts (last: {last}). "
            "Every verse already synthesised is kept; run again to continue.",
            retry_after_s=retry_after,
        )

    def _audio_from(self, response: httpx.Response) -> bytes:
        cfg = self.cfg
        if cfg.audio_response == "raw":
            return response.content
        try:
            encoded = _walk(response.json(), cfg.audio_json_path)
            return base64.b64decode(encoded)
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderResponseError(
                self._redact(
                    f"{cfg.name}: no base64 audio at {cfg.audio_json_path!r} in the reply: {exc}"
                )
            ) from exc


# ---------------------------------------------------------------- configuration


def configured_providers(settings: Settings) -> dict[str, HTTPTTSConfig]:
    """Every `voice_providers:` entry, validated, keyed by its `name`."""
    configs = [HTTPTTSConfig.model_validate(entry) for entry in settings.voice_providers]
    return {cfg.name: cfg for cfg in configs}


def configured_provider(name: str, settings: Settings) -> HTTPTTSProvider | None:
    """The provider `name` resolves to through `voice_providers:`, or `None`.

    Called by `providers.get_provider` when no registered class carries the name,
    which is what makes a YAML entry selectable in `provider_chains["tts"]`.
    """
    cfg = configured_providers(settings).get(name)
    return None if cfg is None else HTTPTTSProvider(cfg)
