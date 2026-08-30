"""Groq chat completions over the OpenAI-compatible endpoint.

**M0 finding 1.** `llama-3.3-70b-versatile` and `llama-3.1-8b-instant` are gone
from Groq's catalogue. Nothing here may hardcode a model id or accept whatever
`/openai/v1/models` happens to list first — see `HttpLLMProvider._resolve_model`.
"""

from typing import Any

from videomaker.providers import register
from videomaker.providers.errors import ProviderResponseError
from videomaker.providers.llm import HttpLLMProvider

PROVIDER_NAME = "groq"
GROQ_BASE_URL = "https://api.groq.com"
GROQ_MODELS_PATH = "/openai/v1/models"
GROQ_CHAT_PATH = "/openai/v1/chat/completions"

# Ordered best-first. Resolution intersects this with the live catalogue; when
# the intersection is empty the provider fails loudly instead of substituting.
GROQ_MODEL_PREFERENCE: tuple[str, ...] = (
    "openai/gpt-oss-120b",
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-20b",
    "groq/compound",
)


@register("llm", PROVIDER_NAME)
class GroqProvider(HttpLLMProvider):
    """Groq LLM provider; model id resolved at runtime, never hardcoded."""

    provider_name = PROVIDER_NAME
    preference = GROQ_MODEL_PREFERENCE

    def _api_key(self) -> str:
        return self.settings.groq_api_key

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._require_api_key()}",
            "Content-Type": "application/json",
        }

    def _catalogue(self) -> list[str]:
        response = self.client.get(f"{GROQ_BASE_URL}{GROQ_MODELS_PATH}", headers=self._headers())
        self._raise_for_status(response)
        data = self._json(response).get("data")
        if not isinstance(data, list):
            raise ProviderResponseError("groq /models did not return a data array")
        return [
            str(entry["id"])
            for entry in data
            if isinstance(entry, dict) and isinstance(entry.get("id"), str)
        ]

    def _error_signal(self, body: dict[str, Any]) -> str:
        error = body.get("error")
        if not isinstance(error, dict):
            return str(error or "")
        return " ".join(str(error.get(field, "")) for field in ("code", "type", "message")).strip()

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
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_schema is not None:
            payload["response_format"] = {"type": "json_object"}
        response = self.client.post(
            f"{GROQ_BASE_URL}{GROQ_CHAT_PATH}", headers=self._headers(), json=payload
        )
        self._raise_for_status(response)
        choices = self._json(response).get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderResponseError("groq returned no choices")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise ProviderResponseError("groq returned an empty completion")
        return content
