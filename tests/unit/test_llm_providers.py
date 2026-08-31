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
        {
            "name": f"models/{GEMINI_MODEL_PREFERENCE[0]}",
            "supportedGenerationMethods": ["generateContent"],
        },
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
    assert p._resolve_model() == GEMINI_MODEL_PREFERENCE[0]


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
    assert result.model == GEMINI_MODEL_PREFERENCE[0]


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
    assert GEMINI_MODEL_PREFERENCE[0] in generate.url.path


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


def test_a_recorded_llm_call_survives_a_new_quota_tracker(tmp_path):
    """`record()` only mutates memory; without a `save()` the count dies with the job.

    `build_deps` builds a fresh `QuotaTracker` per job, so an unsaved count means
    gemini's per-day budget can never be enforced across invocations — every run
    starts the daily counter at zero. Regression for M2 spike follow-up 1.
    """

    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=MODELS_BODY)
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    ledger = tmp_path / "quota.json"
    provider = GroqProvider(Settings(groq_api_key="k"), client=_client(handler))
    provider.quota = QuotaTracker(ledger)
    provider.generate(system="s", user="u")

    budget = SOFT_BUDGETS["groq"]
    spent_in_memory = provider.quota.remaining("groq", budget)
    reloaded = QuotaTracker(ledger).remaining("groq", budget)
    assert reloaded == spent_in_memory, "the recorded call did not reach disk"


# ------------------------------------------- a listed model that will not answer

#: `ListModels` is a catalogue, not a promise. Measured against the live API on
#: 2026-08-31 with this project's own key: every one of the four ids the old
#: `GEMINI_MODEL_PREFERENCE` named answered `generateContent` with a 404, and the
#: first two of them were *still advertised by `ListModels`* while doing it.
#:
#: ```
#: gemini-2.5-flash       404 NOT_FOUND  no longer available to new users
#: gemini-2.5-flash-lite  404 NOT_FOUND  no longer available to new users
#: gemini-2.0-flash       404 NOT_FOUND  no longer available
#: gemini-2.0-flash-lite  404 NOT_FOUND  no longer available
#: ```
#:
#: So resolution that trusts the catalogue "succeeds" and every call fails — which
#: is exactly how M3 Task 14 spent 240 requests on a provider that never answered.
RETIRED_IDS = ("gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash", "gemini-2.0-flash-lite")


def test_the_gemini_preference_names_no_model_measured_dead():
    """A regression pin on the list itself, with the evidence in `RETIRED_IDS`."""
    assert not set(GEMINI_MODEL_PREFERENCE) & set(RETIRED_IDS)
    assert GEMINI_MODEL_PREFERENCE  # and it still names something


def _retirement(model: str) -> dict:
    return {
        "error": {
            "code": 404,
            "status": "NOT_FOUND",
            "message": (
                f"This model models/{model} is no longer available to new users. "
                "Please update your code to use models/gemini-3.6-flash."
            ),
        }
    }


def _catalogue_of(*models: str) -> dict:
    return {
        "models": [
            {"name": f"models/{m}", "supportedGenerationMethods": ["generateContent"]}
            for m in models
        ]
    }


def _retiring_handler(dead: set[str], calls: list[httpx.Request] | None = None):
    """Lists the whole preference; 404s the ids in `dead`, answers for the rest."""

    def handler(request):
        if calls is not None:
            calls.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=_catalogue_of(*GEMINI_MODEL_PREFERENCE))
        model = request.url.path.rsplit("/", 1)[-1].split(":")[0]
        if model in dead:
            return httpx.Response(404, json=_retirement(model))
        return httpx.Response(200, json=_gemini_text("hi"))

    return handler


def test_a_listed_but_retired_model_falls_through_to_the_next_preference():
    """The catalogue said yes and `generateContent` said 404. Only the call is proof."""
    calls: list[httpx.Request] = []
    p = GeminiProvider(
        Settings(gemini_api_key="k"),
        client=_client(_retiring_handler({GEMINI_MODEL_PREFERENCE[0]}, calls)),
    )
    result = p.generate(system="s", user="u")

    assert result.text == "hi"
    assert result.model == GEMINI_MODEL_PREFERENCE[1]
    tried = [c.url.path.rsplit("/", 1)[-1].split(":")[0] for c in calls if ":generateContent" in c.url.path]
    assert tried == [GEMINI_MODEL_PREFERENCE[0], GEMINI_MODEL_PREFERENCE[1]]


def test_a_retired_model_is_not_offered_again_within_the_process():
    """One 404 per dead id, not one per request. 240 of them is the bug being fixed."""
    calls: list[httpx.Request] = []
    p = GeminiProvider(
        Settings(gemini_api_key="k"),
        client=_client(_retiring_handler({GEMINI_MODEL_PREFERENCE[0]}, calls)),
    )
    p.generate(system="s", user="u1")
    p.generate(system="s", user="u2")

    tried = [c.url.path.rsplit("/", 1)[-1].split(":")[0] for c in calls if ":generateContent" in c.url.path]
    assert tried.count(GEMINI_MODEL_PREFERENCE[0]) == 1
    assert tried.count(GEMINI_MODEL_PREFERENCE[1]) == 2


def test_every_preferred_model_retired_is_a_loud_config_error():
    """The M0 rule survives: never adopt an arbitrary id, say what happened instead."""
    p = GeminiProvider(
        Settings(gemini_api_key="k"),
        client=_client(_retiring_handler(set(GEMINI_MODEL_PREFERENCE))),
    )
    with pytest.raises(ProviderConfigError) as exc:
        p.generate(system="s", user="u")
    message = str(exc.value)
    assert GEMINI_MODEL_PREFERENCE[0] in message
    assert "retired" in message.lower() or "refused" in message.lower()


def test_a_404_on_a_retired_model_costs_no_quota(tmp_path):
    """The half of M3 Task 14 that cost the user their day.

    A `generateContent` 404 is refused before any model runs: Google books no unit
    for it, so neither may we. The successful retry costs exactly one — not two.
    """
    ledger = tmp_path / "quota.json"
    p = GeminiProvider(
        Settings(gemini_api_key="k"),
        client=_client(_retiring_handler({GEMINI_MODEL_PREFERENCE[0]})),
        quota=QuotaTracker(ledger),
    )
    p.generate(system="s", user="u")

    budget = SOFT_BUDGETS["gemini"]
    assert QuotaTracker(ledger).remaining("gemini", budget)["per_day"] == budget.per_day - 1


def test_a_dead_preference_list_spends_nothing_at_all(tmp_path):
    """Every id 404s: four refused requests, zero units. The 240 that must not recur."""
    ledger = tmp_path / "quota.json"
    p = GeminiProvider(
        Settings(gemini_api_key="k"),
        client=_client(_retiring_handler(set(GEMINI_MODEL_PREFERENCE))),
        quota=QuotaTracker(ledger),
    )
    with pytest.raises(ProviderConfigError):
        p.generate(system="s", user="u")

    budget = SOFT_BUDGETS["gemini"]
    assert QuotaTracker(ledger).remaining("gemini", budget)["per_day"] == budget.per_day


def test_a_429_still_costs_quota(tmp_path):
    """The other side of the rule, and the mutant guard on it.

    A rate limit or a 500 *left the machine* and probably counted against the
    allowance. Only a refusal that never reached a model is free; a fix that stops
    booking failures wholesale would put us back to billing past the free tier.
    """
    ledger = tmp_path / "quota.json"

    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=_catalogue_of(*GEMINI_MODEL_PREFERENCE))
        return httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED", "message": "slow down"}})

    p = GeminiProvider(
        Settings(gemini_api_key="k"), client=_client(handler), quota=QuotaTracker(ledger)
    )
    with pytest.raises(TransientError):
        p.generate(system="s", user="u")

    budget = SOFT_BUDGETS["gemini"]
    assert QuotaTracker(ledger).remaining("gemini", budget)["per_day"] == budget.per_day - 1
