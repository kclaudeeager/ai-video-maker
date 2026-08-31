"""Google Gemini via the Generative Language API — the second link in the LLM chain.

Same resolution discipline as Groq (**M0 finding 1**): the catalogue is fetched
from `ListModels`, filtered to models that actually support `generateContent`,
and intersected with an ordered preference list. Google retires model aliases on
a published schedule, so a hardcoded id is the same latent config bug here.

Wire differences from Groq: the key travels in an `x-goog-api-key` header (never
the query string, which leaks into logs and proxies), JSON mode is
`generationConfig.responseMimeType`, and errors arrive as
`{"error": {"code", "status", "message", "details": [...]}}` with the retry hint
in a `RetryInfo` detail rather than a `Retry-After` header.
"""

from typing import Any

from videomaker.providers import register
from videomaker.providers.errors import ProviderResponseError
from videomaker.providers.llm import HttpLLMProvider, parse_retry_after

PROVIDER_NAME = "gemini"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com"
GEMINI_API_VERSION = "v1beta"
GENERATE_CONTENT_METHOD = "generateContent"
MODEL_NAME_PREFIX = "models/"
MODELS_PAGE_SIZE = 200

#: Models this account can actually *call*, newest first, flash before flash-lite.
#:
#: Every id in the previous list was dead. Probed against the live API on
#: 2026-08-31 with a one-token `generateContent`:
#:
#: ```
#: gemini-2.5-flash        404  listed by ListModels, "no longer available to new users"
#: gemini-2.5-flash-lite   404  listed by ListModels, "no longer available to new users"
#: gemini-2.0-flash        404  not listed at all
#: gemini-2.0-flash-lite   404  not listed at all
#: gemini-3.6-flash        OK   <- Google's own named replacement for 2.5-flash
#: gemini-3.5-flash        OK
#: gemini-3.5-flash-lite   OK   <- Google's own named replacement for 2.5-flash-lite
#: gemini-3.1-flash-lite   OK
#: gemini-3.7-flash        read timeout, twice
#: gemini-flash-latest     503 UNAVAILABLE / read timeout
#: ```
#:
#: The first two are the whole of M3 Task 14's defect 1: `ListModels` advertises
#: them, so resolution succeeded and all 240 calls 404'd. Fixing the list is only
#: half the repair — Google retires on a published schedule and this list will rot
#: again — so `HttpLLMProvider._with_model_fallback` treats a 404 as proof and walks
#: on. The list decides *which* working model; the fallback decides *that* it works.
#:
#: Excluded on purpose: `gemini-3.7-flash` and `gemini-flash-latest`, which were
#: under load both times they were probed and must not be led with; `-preview` ids,
#: which can vanish without a deprecation window; and the floating `-latest`
#: aliases, because the response cache keys entries by model id and an alias that
#: silently moves makes those keys lie about what produced the text.
GEMINI_MODEL_PREFERENCE: tuple[str, ...] = (
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
)


@register("llm", PROVIDER_NAME)
class GeminiProvider(HttpLLMProvider):
    """Gemini LLM provider; model id resolved at runtime, never hardcoded."""

    provider_name = PROVIDER_NAME
    preference = GEMINI_MODEL_PREFERENCE

    def _api_key(self) -> str:
        return self.settings.gemini_api_key

    def _headers(self) -> dict[str, str]:
        return {"x-goog-api-key": self._require_api_key(), "Content-Type": "application/json"}

    def _catalogue(self) -> list[str]:
        response = self.client.get(
            f"{GEMINI_BASE_URL}/{GEMINI_API_VERSION}/models",
            headers=self._headers(),
            params={"pageSize": MODELS_PAGE_SIZE},
        )
        self._raise_for_status(response)
        models = self._json(response).get("models")
        if not isinstance(models, list):
            raise ProviderResponseError("gemini ListModels did not return a models array")
        available: list[str] = []
        for entry in models:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                continue
            methods = entry.get("supportedGenerationMethods")
            if isinstance(methods, list) and GENERATE_CONTENT_METHOD not in methods:
                continue  # embedding-only models can never answer a prompt
            available.append(str(entry["name"]).removeprefix(MODEL_NAME_PREFIX))
        return available

    def _error_signal(self, body: dict[str, Any]) -> str:
        error = body.get("error")
        if not isinstance(error, dict):
            return str(error or "")
        parts = [str(error.get("status", "")), str(error.get("message", ""))]
        for detail in self._details(body):
            for violation in detail.get("violations", []):
                if isinstance(violation, dict):
                    parts.append(str(violation.get("quotaId", "")))
        return " ".join(part for part in parts if part).strip()

    def _retry_delay(self, body: dict[str, Any]) -> float | None:
        for detail in self._details(body):
            delay = parse_retry_after(detail.get("retryDelay"))
            if delay is not None:
                return delay
        return None

    @staticmethod
    def _details(body: dict[str, Any]) -> list[dict[str, Any]]:
        error = body.get("error")
        details = error.get("details") if isinstance(error, dict) else None
        if not isinstance(details, list):
            return []
        return [detail for detail in details if isinstance(detail, dict)]

    def _complete(
        self,
        *,
        model: str,
        system: str,
        user: str,
        json_schema: dict | None,
        temperature: float,
        max_tokens: int,
    ) -> str:
        generation_config: dict[str, Any] = {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        }
        if json_schema is not None:
            # Only the MIME type: `responseSchema` speaks an OpenAPI subset, not
            # JSON Schema, and rejects payloads our callers legitimately produce.
            generation_config["responseMimeType"] = "application/json"
        payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": generation_config,
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        response = self.client.post(
            f"{GEMINI_BASE_URL}/{GEMINI_API_VERSION}/models/{model}:{GENERATE_CONTENT_METHOD}",
            headers=self._headers(),
            json=payload,
        )
        self._raise_for_status(response)
        return self._first_text(self._json(response))

    @staticmethod
    def _first_text(body: dict[str, Any]) -> str:
        candidates = body.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise ProviderResponseError("gemini returned no candidates")
        first = candidates[0] if isinstance(candidates[0], dict) else {}
        content = first.get("content") if isinstance(first, dict) else None
        parts = content.get("parts") if isinstance(content, dict) else None
        texts = [
            part["text"]
            for part in parts or []
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ]
        text = "".join(texts)
        if not text.strip():
            reason = first.get("finishReason", "unknown")
            raise ProviderResponseError(f"gemini returned no text (finishReason={reason})")
        return text
