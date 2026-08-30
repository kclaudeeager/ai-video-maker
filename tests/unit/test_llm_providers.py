import httpx
import pytest

from videomaker.cache import ResponseCache
from videomaker.config import Settings
from videomaker.providers.errors import ProviderConfigError, QuotaExceeded, TransientError
from videomaker.providers.llm.gemini import GEMINI_MODEL_PREFERENCE, GeminiProvider
from videomaker.providers.llm.groq import GROQ_MODEL_PREFERENCE, GroqProvider
from videomaker.providers.ratelimit import SOFT_BUDGETS, QuotaTracker

MODELS_BODY = {"data": [{"id": "allam-2-7b"}, {"id": "openai/gpt-oss-120b"}, {"id": "whisper-large-v3"}]}
ONLY_JUNK = {"data": [{"id": "allam-2-7b"}, {"id": "whisper-large-v3"}]}


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_resolves_preferred_model_not_first_listed():
    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=MODELS_BODY)
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    p = GroqProvider(Settings(groq_api_key="k"), client=_client(handler))
    assert p._resolve_model() == "openai/gpt-oss-120b"


def test_raises_when_no_preferred_model_available():
    def handler(request):
        return httpx.Response(200, json=ONLY_JUNK)

    p = GroqProvider(Settings(groq_api_key="k"), client=_client(handler))
    with pytest.raises(ProviderConfigError) as exc:
        p._resolve_model()
    message = str(exc.value)
    assert "allam-2-7b" in message                      # what the account has
    assert GROQ_MODEL_PREFERENCE[0] in message          # what we wanted
    assert "allam-2-7b" not in (p.__dict__.get("model") or "")  # never silently adopted


def test_generate_returns_text_and_marks_uncached():
    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=MODELS_BODY)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"scenes": []}'}}]})

    p = GroqProvider(Settings(groq_api_key="k"), client=_client(handler))
    result = p.generate(system="s", user="u")
    assert result.text == '{"scenes": []}'
    assert result.cached is False
    assert result.model == "openai/gpt-oss-120b"


def test_429_maps_to_transient_with_retry_after():
    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=MODELS_BODY)
        return httpx.Response(429, headers={"Retry-After": "12"}, json={})

    p = GroqProvider(Settings(groq_api_key="k"), client=_client(handler))
    with pytest.raises(TransientError) as exc:
        p.generate(system="s", user="u")
    assert exc.value.retry_after_s == 12


def test_missing_key_fails_fast_as_config_error():
    p = GroqProvider(Settings(groq_api_key=""), client=_client(lambda r: httpx.Response(200)))
    with pytest.raises(ProviderConfigError):
        p.generate(system="s", user="u")


def test_daily_quota_exhaustion_maps_to_quota_exceeded():
    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=MODELS_BODY)
        return httpx.Response(429, json={"error": {"code": "daily_limit_exceeded"}})

    p = GroqProvider(Settings(groq_api_key="k"), client=_client(handler))
    with pytest.raises(QuotaExceeded):
        p.generate(system="s", user="u")


# --------------------------------------------------------------------------- Groq extras


def _groq_recorder(calls, *, content='{"ok": true}'):
    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=MODELS_BODY)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return handler


def test_groq_sends_bearer_auth_and_json_object_format_for_schema():
    calls: list[httpx.Request] = []
    p = GroqProvider(Settings(groq_api_key="k"), client=_client(_groq_recorder(calls)))
    p.generate(system="s", user="u", json_schema={"type": "object"})
    chat = next(c for c in calls if c.url.path.endswith("/chat/completions"))
    assert chat.headers["authorization"] == "Bearer k"
    body = httpx.Response(200, content=chat.content).json()
    assert body["response_format"] == {"type": "json_object"}
    assert body["model"] == "openai/gpt-oss-120b"


def test_cache_hit_skips_the_network_and_never_touches_quota(tmp_path):
    calls: list[httpx.Request] = []
    cache = ResponseCache(tmp_path / "responses")
    quota = QuotaTracker(tmp_path / "quota.json")
    budget = SOFT_BUDGETS["groq"]

    def build():
        return GroqProvider(
            Settings(groq_api_key="k"),
            client=_client(_groq_recorder(calls)),
            cache=cache,
            quota=quota,
        )

    first = build().generate(system="s", user="u")
    assert first.cached is False
    spent_after_first = quota.remaining("groq", budget)
    chat_calls = len([c for c in calls if c.url.path.endswith("/chat/completions")])

    second = build().generate(system="s", user="u")
    assert second.cached is True
    assert second.text == first.text
    assert second.model == first.model
    assert len([c for c in calls if c.url.path.endswith("/chat/completions")]) == chat_calls
    assert quota.remaining("groq", budget) == spent_after_first


def test_groq_5xx_maps_to_transient():
    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=MODELS_BODY)
        return httpx.Response(503, text="upstream unavailable")

    p = GroqProvider(Settings(groq_api_key="k"), client=_client(handler))
    with pytest.raises(TransientError):
        p.generate(system="s", user="u")


def test_groq_unusable_payload_maps_to_response_error():
    from videomaker.providers.errors import ProviderResponseError

    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=MODELS_BODY)
        return httpx.Response(200, json={"choices": []})

    p = GroqProvider(Settings(groq_api_key="k"), client=_client(handler))
    with pytest.raises(ProviderResponseError):
        p.generate(system="s", user="u")


# --------------------------------------------------------------------------- Gemini

GEMINI_MODELS_BODY = {
    "models": [
        {"name": "models/embedding-001", "supportedGenerationMethods": ["embedContent"]},
        {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
    ]
}
# `gemini-1.0-pro-vision-latest` is Gemini's `allam-2-7b`: a real chat model that
# a "first one that works" fallback would happily and wrongly adopt.
GEMINI_ONLY_JUNK = {
    "models": [
        {"name": "models/embedding-001", "supportedGenerationMethods": ["embedContent"]},
        {
            "name": "models/gemini-1.0-pro-vision-latest",
            "supportedGenerationMethods": ["generateContent"],
        },
    ]
}


def _gemini_text(text):
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


def test_gemini_resolves_preferred_model_not_first_listed():
    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=GEMINI_MODELS_BODY)
        return httpx.Response(200, json=_gemini_text("hi"))

    p = GeminiProvider(Settings(gemini_api_key="k"), client=_client(handler))
    assert p._resolve_model() == "gemini-2.5-flash"


def test_gemini_raises_when_no_preferred_model_available():
    def handler(request):
        return httpx.Response(200, json=GEMINI_ONLY_JUNK)

    p = GeminiProvider(Settings(gemini_api_key="k"), client=_client(handler))
    with pytest.raises(ProviderConfigError) as exc:
        p._resolve_model()
    message = str(exc.value)
    assert "gemini-1.0-pro-vision-latest" in message    # what the account has
    assert GEMINI_MODEL_PREFERENCE[0] in message        # what we wanted
    assert "embedding-001" not in message               # can never answer a prompt: noise
    assert "gemini-1.0" not in (p.__dict__.get("model") or "")  # never silently adopted


def test_gemini_generate_returns_text_and_marks_uncached():
    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=GEMINI_MODELS_BODY)
        return httpx.Response(200, json=_gemini_text('{"scenes": []}'))

    p = GeminiProvider(Settings(gemini_api_key="k"), client=_client(handler))
    result = p.generate(system="s", user="u")
    assert result.text == '{"scenes": []}'
    assert result.cached is False
    assert result.model == "gemini-2.5-flash"


def test_gemini_uses_goog_api_key_header_and_response_mime_type():
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=GEMINI_MODELS_BODY)
        return httpx.Response(200, json=_gemini_text("{}"))

    p = GeminiProvider(Settings(gemini_api_key="k"), client=_client(handler))
    p.generate(system="s", user="u", json_schema={"type": "object"})
    generate = next(c for c in calls if c.url.path.endswith(":generateContent"))
    assert generate.headers["x-goog-api-key"] == "k"
    assert "authorization" not in generate.headers
    assert "key=" not in str(generate.url)  # the key never rides in the query string
    body = httpx.Response(200, content=generate.content).json()
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert "gemini-2.5-flash" in generate.url.path


def test_gemini_429_maps_to_transient_with_retry_delay():
    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=GEMINI_MODELS_BODY)
        return httpx.Response(
            429,
            json={
                "error": {
                    "code": 429,
                    "status": "RESOURCE_EXHAUSTED",
                    "message": "Resource has been exhausted (e.g. check quota).",
                    "details": [
                        {
                            "@type": "type.googleapis.com/google.rpc.RetryInfo",
                            "retryDelay": "12s",
                        }
                    ],
                }
            },
        )

    p = GeminiProvider(Settings(gemini_api_key="k"), client=_client(handler))
    with pytest.raises(TransientError) as exc:
        p.generate(system="s", user="u")
    assert exc.value.retry_after_s == 12


def test_gemini_missing_key_fails_fast_as_config_error():
    p = GeminiProvider(Settings(gemini_api_key=""), client=_client(lambda r: httpx.Response(200)))
    with pytest.raises(ProviderConfigError):
        p.generate(system="s", user="u")


def test_gemini_daily_quota_exhaustion_maps_to_quota_exceeded():
    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=GEMINI_MODELS_BODY)
        return httpx.Response(
            429,
            json={
                "error": {
                    "code": 429,
                    "status": "RESOURCE_EXHAUSTED",
                    "message": "Quota exceeded for quota metric 'Generate requests per day'",
                    "details": [
                        {
                            "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                            "violations": [
                                {"quotaId": "GenerateRequestsPerDayPerProjectPerModel"}
                            ],
                        }
                    ],
                }
            },
        )

    p = GeminiProvider(Settings(gemini_api_key="k"), client=_client(handler))
    with pytest.raises(QuotaExceeded):
        p.generate(system="s", user="u")


def test_gemini_403_maps_to_config_error():
    def handler(request):
        return httpx.Response(
            403, json={"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "bad key"}}
        )

    p = GeminiProvider(Settings(gemini_api_key="k"), client=_client(handler))
    with pytest.raises(ProviderConfigError):
        p.generate(system="s", user="u")


def test_both_providers_are_registered_under_their_chain_names():
    from videomaker.providers import get_provider

    assert isinstance(get_provider("llm", "groq", Settings(groq_api_key="k")), GroqProvider)
    assert isinstance(get_provider("llm", "gemini", Settings(gemini_api_key="k")), GeminiProvider)
