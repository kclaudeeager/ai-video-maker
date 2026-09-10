"""The budget guard: a paid run is estimated first and refused past the budget
unless someone confirmed it. A whole Bible at $0.10/minute is ~$527; this is the
only thing between one command and that bill.
"""

import wave
from pathlib import Path

import httpx
import pytest

from videomaker.providers.ratelimit import QuotaTracker
from videomaker.providers.tts.http_api import (
    HTTPTTSConfig,
    HTTPTTSProvider,
    TTSBudget,
    TTSBudgetExceeded,
    check_budget,
    estimate_minutes,
)

LONG_TEXT = " ".join(["word"] * 1500)  # ten minutes at 150 wpm


def wav_bytes() -> bytes:
    tmp = Path(__file__).parent / ".tmp_budget.wav"
    with wave.open(str(tmp), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(bytes(800))
    data = tmp.read_bytes()
    tmp.unlink()
    return data


def paid(**overrides) -> HTTPTTSConfig:
    return HTTPTTSConfig(
        **{
            "name": "paid",
            "endpoint": "https://voice.invalid/speak",
            "cost_per_minute_usd": 0.10,
            **overrides,
        }
    )


@pytest.fixture
def provider(tmp_path):
    def build(cfg: HTTPTTSConfig, budget: TTSBudget):
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, content=wav_bytes())

        instance = HTTPTTSProvider(cfg, budget=budget)
        instance.client = httpx.Client(transport=httpx.MockTransport(handler))
        instance.quota = QuotaTracker(tmp_path / "quota.json")
        instance.calls = calls
        return instance

    return build


def test_estimate_is_words_over_wpm():
    assert estimate_minutes(LONG_TEXT) == pytest.approx(10.0)
    assert estimate_minutes(LONG_TEXT, wpm=300) == pytest.approx(5.0)
    assert estimate_minutes("") == 0.0


def test_a_run_over_the_minute_limit_raises_and_makes_no_call(provider, tmp_path):
    instance = provider(paid(), TTSBudget(max_minutes_per_run=5.0, max_usd_per_run=100.0))
    with pytest.raises(TTSBudgetExceeded) as exc:
        instance.synthesize(text=LONG_TEXT, voice="v", out_path=tmp_path / "a.wav")
    message = str(exc.value)
    assert "10.0 minutes" in message
    assert "5-minute limit" in message
    assert "--yes" in message
    assert instance.calls == []
    assert not (tmp_path / "a.wav").exists()


def test_a_run_over_the_usd_limit_raises_naming_the_cost(provider, tmp_path):
    instance = provider(paid(), TTSBudget(max_minutes_per_run=60.0, max_usd_per_run=0.50))
    with pytest.raises(TTSBudgetExceeded, match=r"\$1\.00, over the \$0\.50 limit"):
        instance.synthesize(text=LONG_TEXT, voice="v", out_path=tmp_path / "a.wav")
    assert instance.calls == []


def test_the_same_run_confirmed_proceeds(provider, tmp_path):
    instance = provider(paid(), TTSBudget(max_minutes_per_run=5.0, confirmed=True))
    instance.synthesize(text=LONG_TEXT, voice="v", out_path=tmp_path / "a.wav")
    assert len(instance.calls) == 1


def test_a_zero_cost_provider_ignores_the_usd_limit(provider, tmp_path):
    free = paid(cost_per_minute_usd=0.0)
    instance = provider(free, TTSBudget(max_minutes_per_run=60.0, max_usd_per_run=0.0))
    instance.synthesize(text=LONG_TEXT, voice="v", out_path=tmp_path / "a.wav")
    assert len(instance.calls) == 1


def test_the_minutes_add_up_across_a_run(provider, tmp_path):
    # Each verse is under the limit on its own; the run is not.
    instance = provider(paid(), TTSBudget(max_minutes_per_run=1.5, max_usd_per_run=100.0))
    verse = " ".join(["word"] * 150)  # one minute
    instance.synthesize(text=verse, voice="v", out_path=tmp_path / "1.wav")
    with pytest.raises(TTSBudgetExceeded):
        instance.synthesize(text=verse, voice="v", out_path=tmp_path / "2.wav")
    assert len(instance.calls) == 1


def test_check_budget_is_the_guard_a_caller_can_run_up_front():
    check_budget(4.0, cfg=paid(), budget=TTSBudget(max_minutes_per_run=5.0, max_usd_per_run=1.0))
    with pytest.raises(TTSBudgetExceeded):
        check_budget(6.0, cfg=paid(), budget=TTSBudget(max_minutes_per_run=5.0))
    check_budget(6.0, cfg=paid(), budget=TTSBudget(max_minutes_per_run=5.0, confirmed=True))
