"""Provider ABCs, the decorator registry and ordered fallback chains."""

from collections.abc import Callable

from videomaker.config import Settings
from videomaker.providers.base import (
    ImageProvider,
    LLMProvider,
    LLMResult,
    StockProvider,
    STTProvider,
    TTSProvider,
    TTSResult,
    Uploader,
)
from videomaker.providers.errors import (
    ProviderConfigError,
    ProviderError,
    ProviderResponseError,
    QuotaExceeded,
    TransientError,
)

__all__ = [
    "ImageProvider",
    "LLMProvider",
    "LLMResult",
    "ProviderConfigError",
    "ProviderError",
    "ProviderResponseError",
    "QuotaExceeded",
    "STTProvider",
    "StockProvider",
    "TTSProvider",
    "TTSResult",
    "TransientError",
    "Uploader",
    "get_provider",
    "register",
    "resolve_chain",
]

_REGISTRY: dict[tuple[str, str], type] = {}


def register(kind: str, name: str) -> Callable[[type], type]:
    """Class decorator adding a provider implementation to the registry."""

    def decorator(cls: type) -> type:
        key = (kind, name)
        if key in _REGISTRY:
            raise ProviderConfigError(
                f"provider {kind}/{name} is already registered as {_REGISTRY[key].__name__}"
            )
        _REGISTRY[key] = cls
        return cls

    return decorator


def known_providers(kind: str) -> list[str]:
    """Names registered for `kind`, sorted, for error messages and `doctor`."""
    return sorted(n for (k, n) in _REGISTRY if k == kind)


def get_provider(kind: str, name: str, settings: Settings) -> object:
    """Instantiate the registered provider, passing it the settings object."""
    try:
        cls = _REGISTRY[(kind, name)]
    except KeyError:
        known = ", ".join(known_providers(kind)) or "none"
        raise ProviderConfigError(
            f"unknown {kind} provider {name!r}; known {kind} providers: {known}"
        ) from None
    return cls(settings)


def resolve_chain(kind: str, settings: Settings) -> list[str]:
    """Ordered list of provider names to try for `kind`."""
    return list(settings.provider_chains.get(kind, []))


# Concrete provider modules are imported here so their @register decorators run.
# Each import is guarded: a missing optional dependency (or missing credentials at
# import time) must degrade to "provider unavailable", never an ImportError at CLI
# startup. Tasks 8-10 add their modules to this list.
_CONCRETE_MODULES: tuple[str, ...] = ("videomaker.providers.mock",)

for _module in _CONCRETE_MODULES:  # pragma: no cover - grows from Task 7 onwards
    try:
        __import__(_module)
    except ImportError:
        pass
