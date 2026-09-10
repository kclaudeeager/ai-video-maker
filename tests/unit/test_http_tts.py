"""The vendor-neutral HTTP voice provider: the request it builds, the key it keeps
out of every message, and the rate limiter that paces rather than reacts.

Every HTTP call goes through `httpx.MockTransport`; nothing here touches the
network, and every sleep is captured rather than slept. The clock is a fake fed
to the `QuotaTracker`, and the fake sleep advances it — that is what lets the
pacing tests assert *how long* a wait was without waiting.
"""

import base64
import json
import wave
from pathlib import Path

import httpx
import pytest

from videomaker.config import Settings, load_settings
from videomaker.providers import get_provider
from videomaker.providers.errors import ProviderConfigError, ProviderResponseError
from videomaker.providers.ratelimit import QuotaTracker
from videomaker.providers.tts import http_api
from videomaker.providers.tts.http_api import (
    HTTPTTSConfig,
    HTTPTTSProvider,
    RateLimited,
    TTSBudget,
)

KEY = "sk-live-SECRETSECRETSECRET"
KEY_ENV = "TEST_VOICE_API_KEY"
SAMPLE_RATE = 22050


def wav_bytes(seconds: float = 0.5) -> bytes:
    tmp = Path(__file__).parent / ".tmp_probe.wav"
    with wave.open(str(tmp), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(bytes(int(seconds * SAMPLE_RATE) * 2))
    data = tmp.read_bytes()
    tmp.unlink()
    return data


def config(**overrides) -> HTTPTTSConfig:
    return HTTPTTSConfig(
        **{
            "name": "vendor",
            "endpoint": "https://voice.invalid/v1/speak",
            "api_key_env": KEY_ENV,
            "voices": ["nyira"],
            "languages": ["rw", "sw"],
            **overrides,
        }
    )


class Clock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(http_api, "sleep", clock.sleep)
    monkeypatch.setattr(http_api, "_random", lambda: 1.0)
    return clock


@pytest.fixture
def provider(tmp_path, monkeypatch, clock):
    """A provider over a recording transport. `provider.calls` is every request."""
    monkeypatch.setenv(KEY_ENV, KEY)

    def build(cfg: HTTPTTSConfig, responses=None, budget: TTSBudget | None = None):
        script = list(responses or [])
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            if script:
                status, body, headers = script.pop(0)
                return httpx.Response(status, content=body, headers=headers)
            return httpx.Response(200, content=wav_bytes())

        instance = HTTPTTSProvider(cfg, budget=budget)
        instance.client = httpx.Client(transport=httpx.MockTransport(handler))
        instance.quota = QuotaTracker(tmp_path / "quota.json", clock=clock)
        instance.calls = calls
        return instance

    return build


def speak(instance, text="Words for the vendor to say.", out: Path | None = None):
    return instance.synthesize(
        text=text, voice="nyira", out_path=out or Path("out.wav"), language="rw"
    )


# --------------------------------------------------------------------- request


def test_the_request_is_built_from_the_config(provider, tmp_path):
    instance = provider(config(speed_field="rate", extra_body={"format": "wav"}))
    result = speak(instance, out=tmp_path / "v.wav")
    (request,) = instance.calls
    assert request.method == "POST"
    assert str(request.url) == "https://voice.invalid/v1/speak"
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    assert json.loads(request.content) == {
        "text": "Words for the vendor to say.",
        "voice": "nyira",
        "language": "rw",
        "rate": 1.0,
        "format": "wav",
    }
    assert result.path == tmp_path / "v.wav"
    assert result.sample_rate == SAMPLE_RATE
    assert result.duration_s == pytest.approx(0.5, abs=0.01)


def test_a_custom_auth_header_and_no_speed_field(provider, tmp_path):
    instance = provider(config(auth_header="X-Api-Key", auth_format="{key}"))
    speak(instance, out=tmp_path / "v.wav")
    (request,) = instance.calls
    assert request.headers["X-Api-Key"] == KEY
    assert "Authorization" not in request.headers
    assert "speed" not in json.loads(request.content)


def test_base64_json_audio_is_decoded_along_the_dotted_path(provider, tmp_path):
    payload = {"data": {"audio": [{"content": base64.b64encode(wav_bytes()).decode()}]}}
    instance = provider(
        config(audio_response="base64_json", audio_json_path="data.audio.0.content"),
        responses=[(200, json.dumps(payload).encode(), {})],
    )
    result = speak(instance, out=tmp_path / "v.wav")
    assert result.duration_s == pytest.approx(0.5, abs=0.01)
    with wave.open(str(result.path), "rb") as handle:
        assert handle.getframerate() == SAMPLE_RATE


def test_a_missing_key_names_the_variable_not_the_key(provider, monkeypatch):
    monkeypatch.delenv(KEY_ENV)
    instance = provider(config())
    with pytest.raises(ProviderConfigError, match=KEY_ENV):
        speak(instance)
    assert instance.calls == []


def test_no_auth_at_all_when_no_key_env_is_configured(provider, tmp_path):
    instance = provider(config(api_key_env=""))
    speak(instance, out=tmp_path / "v.wav")
    (request,) = instance.calls
    assert "Authorization" not in request.headers


# ------------------------------------------------------------- the key stays in


def test_the_key_never_appears_in_an_error_message(provider, tmp_path):
    body = f"bad request; you sent {KEY}".encode()
    instance = provider(config(), responses=[(400, body, {})])
    with pytest.raises(ProviderResponseError) as exc:
        speak(instance, out=tmp_path / "v.wav")
    assert KEY not in str(exc.value)
    assert "400" in str(exc.value)


def test_the_key_never_appears_when_retries_run_out(provider, tmp_path):
    body = f"slow down {KEY}".encode()
    instance = provider(config(retry_attempts=2), responses=[(429, body, {})] * 2)
    with pytest.raises(RateLimited) as exc:
        speak(instance, out=tmp_path / "v.wav")
    assert KEY not in str(exc.value)


# ------------------------------------------------------------------ rate limit


def test_the_per_minute_cap_is_paced_by_sleeping_not_by_being_refused(
    provider, clock, tmp_path
):
    instance = provider(config(requests_per_minute=60))
    for n in range(3):
        speak(instance, out=tmp_path / f"{n}.wav")
    assert len(instance.calls) == 3
    # 60/min is one a second: the second and third calls each wait the second
    # the first one did not need.
    assert clock.sleeps == pytest.approx([1.0, 1.0])


def test_no_stated_limit_means_no_sleep(provider, clock, tmp_path):
    instance = provider(config())
    for n in range(3):
        speak(instance, out=tmp_path / f"{n}.wav")
    assert clock.sleeps == []


def test_a_429_with_retry_after_waits_that_long_and_retries(provider, clock, tmp_path):
    instance = provider(config(), responses=[(429, b"busy", {"Retry-After": "2"})])
    result = speak(instance, out=tmp_path / "v.wav")
    assert len(instance.calls) == 2
    assert clock.sleeps == pytest.approx([2.0])
    assert result.duration_s > 0


def test_four_consecutive_429s_raise_rate_limited(provider, clock, tmp_path):
    instance = provider(config(retry_attempts=4), responses=[(429, b"busy", {})] * 4)
    with pytest.raises(RateLimited, match="4 attempts"):
        speak(instance, out=tmp_path / "v.wav")
    assert len(instance.calls) == 4
    # Exponential backoff with full jitter (the fake draw is 1.0, the ceiling).
    assert clock.sleeps == pytest.approx([1.0, 2.0, 4.0, 8.0])


def test_a_5xx_is_retried_the_same_way(provider, tmp_path):
    instance = provider(config(), responses=[(503, b"down", {})])
    speak(instance, out=tmp_path / "v.wav")
    assert len(instance.calls) == 2


def test_a_400_is_not_retried(provider, clock, tmp_path):
    instance = provider(config(), responses=[(400, b"malformed", {})])
    with pytest.raises(ProviderResponseError):
        speak(instance, out=tmp_path / "v.wav")
    assert len(instance.calls) == 1
    assert clock.sleeps == []


def test_a_run_that_would_cross_the_daily_cap_raises_before_the_first_call(
    provider, tmp_path
):
    instance = provider(config(requests_per_day=5))
    instance.quota.record("vendor", 3)
    with pytest.raises(RateLimited, match="2/5 are left") as exc:
        instance.reserve(3)
    assert instance.calls == []
    assert exc.value.retry_after_s is not None
    instance.reserve(2)


def test_the_daily_cap_stops_a_call_with_the_reset_time(provider, tmp_path):
    instance = provider(config(requests_per_day=2))
    instance.quota.record("vendor", 2)
    with pytest.raises(RateLimited, match="resets in"):
        speak(instance, out=tmp_path / "v.wav")
    assert instance.calls == []


def test_the_daily_count_is_in_the_shared_ledger(provider, tmp_path, clock):
    instance = provider(config(requests_per_day=10))
    speak(instance, out=tmp_path / "v.wav")
    again = QuotaTracker(tmp_path / "quota.json", clock=clock)
    assert again.remaining("vendor", http_api.Budget(per_day=10))["per_day"] == 9


def test_max_concurrency_defaults_to_one():
    assert config().max_concurrency == 1


# -------------------------------------------------------------- configuration


def test_a_yaml_voice_provider_is_selectable_by_name(tmp_path, monkeypatch):
    monkeypatch.setenv(KEY_ENV, KEY)
    (tmp_path / "config.yaml").write_text(
        "providers:\n  tts: [vendor]\n"
        "voice_providers:\n"
        "  - name: vendor\n    endpoint: https://voice.invalid/v1/speak\n"
        f"    api_key_env: {KEY_ENV}\n    languages: [rw]\n    voices: [nyira]\n"
    )
    settings = load_settings(tmp_path / "config.yaml")
    assert settings.provider_chains["tts"] == ["vendor"]
    instance = get_provider("tts", "vendor", settings)
    assert isinstance(instance, HTTPTTSProvider)
    assert instance.voices() == ["nyira"]


def test_an_unconfigured_name_is_still_unknown():
    with pytest.raises(ProviderConfigError, match="unknown tts provider"):
        get_provider("tts", "nonesuch", Settings())
