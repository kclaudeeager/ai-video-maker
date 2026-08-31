"""HTTP-backed LLM providers and the plumbing they share.

Groq and Gemini differ only in wire format: the cache key, the quota accounting,
the model-resolution discipline and the error taxonomy are identical, so they
live here in `HttpLLMProvider` and each concrete module supplies four hooks
(`_api_key`, `_catalogue`, `_complete`, `_error_signal`).

**M0 finding 1 lives in `_resolve_model`.** Groq stopped serving the model ids
this project was written against; a "first chat model in the list" fallback
picked `allam-2-7b` (4k context, Arabic-focused) and produced a factually wrong
script that read like a content bug rather than a config bug. Resolution is
therefore *intersection with an ordered preference list, or a loud
`ProviderConfigError`* — never an arbitrary id, in either provider.
"""

from abc import abstractmethod
from typing import Any

import httpx

from videomaker.cache import ResponseCache, hash_inputs
from videomaker.config import Settings
from videomaker.providers.base import LLMProvider, LLMResult
from videomaker.providers.errors import (
    ProviderConfigError,
    ProviderResponseError,
    QuotaExceeded,
    TransientError,
)
from videomaker.providers.ratelimit import SOFT_BUDGETS, QuotaTracker

DEFAULT_TIMEOUT_S = 120.0
MAX_LISTED_MODELS = 24  # keep the "what your account offers" message readable

# Substrings that mark a 429 as a *daily cap* rather than a rate limit. Only a
# daily cap should advance the provider fallback chain; a rate limit is worth a
# retry against the same provider.
DAILY_CAP_MARKERS: tuple[str, ...] = (
    "daily",
    "per day",
    "per-day",
    "requests_per_day",
    "perday",
    "insufficient_quota",
    "quota_exceeded",
)

# Process-lifetime model catalogues, keyed by (provider, api key). Only used
# when the provider owns its client: an injected client is a caller-supplied
# transport (tests, or a future proxy), and one caller's catalogue must never
# leak into another's.
_CATALOGUE_CACHE: dict[tuple[str, str], tuple[str, ...]] = {}


def parse_retry_after(value: str | None) -> float | None:
    """Seconds from a `Retry-After` header or a Google `retryDelay` (`"12s"`)."""
    if not value:
        return None
    try:
        return float(value.removesuffix("s"))
    except ValueError:
        return None


class HttpLLMProvider(LLMProvider):
    """Cache -> quota -> HTTP call, with model resolution shared by both providers."""

    provider_name: str = ""
    preference: tuple[str, ...] = ()

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.Client | None = None,
        cache: ResponseCache | None = None,
        quota: QuotaTracker | None = None,
    ) -> None:
        self.settings = settings
        self.cache = cache
        self.quota = quota
        # `model` stays None until a *preferred* id is resolved; the M0 pin in
        # `test_raises_when_no_preferred_model_available` reads this attribute.
        self.model: str | None = None
        self._client = client
        self._owns_client = client is None

    # ----------------------------------------------------------------- plumbing

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=DEFAULT_TIMEOUT_S)
        return self._client

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    def _require_api_key(self) -> str:
        key = self._api_key()
        if not key:
            raise ProviderConfigError(
                f"{self.provider_name} API key is not set; add it to .env or config.yaml"
            )
        return key

    # ------------------------------------------------------- model resolution

    def _resolve_model(self) -> str:
        """The first preferred model this account actually serves (M0 finding 1)."""
        if self.model is not None:
            return self.model
        key = self._require_api_key()
        cache_key = (self.provider_name, key)
        available = _CATALOGUE_CACHE.get(cache_key) if self._owns_client else None
        if available is None:
            available = tuple(self._catalogue())
            if self._owns_client:
                _CATALOGUE_CACHE[cache_key] = available
        offered = set(available)
        for candidate in self.preference:
            if candidate in offered:
                self.model = candidate
                return candidate
        listed = ", ".join(sorted(available)[:MAX_LISTED_MODELS]) or "nothing"
        raise ProviderConfigError(
            f"{self.provider_name} serves none of the models this project prefers. "
            f"Wanted, in order: {', '.join(self.preference)}. "
            f"Your account offers: {listed}. "
            "Update the preference list rather than accepting an arbitrary model — "
            "a mismatched model silently produces a bad script (M0 finding 1)."
        )

    # ----------------------------------------------------------------- generate

    def generate(
        self,
        *,
        system: str,
        user: str,
        json_schema: dict | None = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> LLMResult:
        self._require_api_key()
        model = self._resolve_model()
        key = hash_inputs(
            provider=self.provider_name,
            model=model,
            system=system,
            user=user,
            temperature=temperature,
            schema=json_schema,
        )
        if self.cache is not None:
            entry = self.cache.get(key)
            text = entry.get("text") if entry else None
            if isinstance(text, str):
                # A hit costs nothing, so the quota tracker is never touched.
                return LLMResult(text=text, model=str(entry.get("model") or model), cached=True)
        if self.quota is not None:
            self.quota.check(self.provider_name, SOFT_BUDGETS[self.provider_name])
        try:
            text = self._complete(
                model=model,
                system=system,
                user=user,
                json_schema=json_schema,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        finally:
            # The request left the machine even if it came back 429 or 500, so
            # book it either way: over-counting costs headroom, never money.
            if self.quota is not None:
                self.quota.record(self.provider_name)
                # `record` only mutates memory, and `build_deps` makes a fresh
                # tracker per job — without this the count dies with the job and
                # a per-day budget (gemini's 240) can never be enforced across
                # invocations. The stock and image providers already save here.
                self.quota.save()
        if self.cache is not None:
            self.cache.put(key, {"text": text, "model": model})
        return LLMResult(text=text, model=model, cached=False)

    # -------------------------------------------------------------- error maps

    def _raise_for_status(self, response: httpx.Response) -> None:
        """Map an HTTP failure onto the provider error taxonomy."""
        if response.is_success:
            return
        body = self._error_body(response)
        signal = self._error_signal(body).lower()
        status = response.status_code
        if status in (401, 403):
            raise ProviderConfigError(
                f"{self.provider_name} rejected the API key ({status}): {signal or 'no detail'}"
            )
        if status == 429:
            if any(marker in signal for marker in DAILY_CAP_MARKERS):
                raise QuotaExceeded(f"{self.provider_name} daily free-tier cap reached: {signal}")
            retry_after = parse_retry_after(
                response.headers.get("Retry-After")
            ) or self._retry_delay(body)
            raise TransientError(
                f"{self.provider_name} rate limited: {signal or 'no detail'}",
                retry_after_s=retry_after,
            )
        if status == 408 or status >= 500:
            raise TransientError(
                f"{self.provider_name} returned {status}: {signal or 'no detail'}",
                retry_after_s=parse_retry_after(response.headers.get("Retry-After")),
            )
        raise ProviderResponseError(
            f"{self.provider_name} returned {status}: {signal or response.text[:200]}"
        )

    @staticmethod
    def _error_body(response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    def _json(self, response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderResponseError(
                f"{self.provider_name} returned a non-JSON body: {response.text[:200]}"
            ) from exc
        if not isinstance(body, dict):
            raise ProviderResponseError(f"{self.provider_name} returned {type(body).__name__}")
        return body

    def _retry_delay(self, body: dict[str, Any]) -> float | None:
        """Provider-specific retry hint carried in the body rather than a header.

        Groq puts it in `Retry-After`; Gemini overrides this with `RetryInfo`.
        """
        _ = body
        return None

    # ---------------------------------------------------------------- subclass

    @abstractmethod
    def _api_key(self) -> str: ...

    @abstractmethod
    def _catalogue(self) -> list[str]:
        """Model ids this account can use, in the provider's own listing order."""

    @abstractmethod
    def _error_signal(self, body: dict[str, Any]) -> str:
        """Flattened error text used to classify a failure."""

    @abstractmethod
    def _complete(
        self,
        *,
        model: str,
        system: str,
        user: str,
        json_schema: dict | None,
        temperature: float,
        max_tokens: int,
    ) -> str: ...
