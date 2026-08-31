class ProviderError(Exception):
    """Base class for every failure raised by a provider implementation."""


class QuotaExceeded(ProviderError):
    """The provider's free-tier allowance is spent; do not retry today."""


class TransientError(ProviderError):
    """A temporary failure (rate limit, 5xx, timeout); retrying may succeed."""

    def __init__(self, message: str = "", retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class ProviderConfigError(ProviderError):
    """The provider is missing credentials, unknown, or misconfigured."""


class ProviderResponseError(ProviderError):
    """The provider answered, but the payload was unusable."""


class ModelRetired(ProviderError):
    """The provider lists this model but will not serve it.

    Its own category because neither of the neighbouring answers is right. The
    account is fine and the provider is fine, so this is not a `ProviderConfigError`
    that should advance the chain; retrying will never help, so it is not a
    `TransientError`; and nothing came back to parse, so it is not a
    `ProviderResponseError`. The correct response is *a different model id* — see
    `HttpLLMProvider._with_model_fallback`.

    It is also the one failure that costs no quota. Google refuses a
    `generateContent` on a retired id before any model runs, so it books no unit
    against the free tier and neither may we (M3 Task 14: 240 units, every one of
    them a 404).
    """
