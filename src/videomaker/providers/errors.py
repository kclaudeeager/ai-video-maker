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
