"""Contract tests for the Pexels stock provider and the Cloudflare Flux image provider.

Every HTTP exchange goes through `httpx.MockTransport`: these tests must never
reach the network, and must never spend a unit of anybody's free tier.
"""

import base64
import json
from pathlib import Path

import httpx
import pytest

from videomaker.cache import ResponseCache
from videomaker.config import Settings
from videomaker.models import Aspect, VisualKind
from videomaker.providers.errors import (
    ProviderConfigError,
    QuotaExceeded,
    TransientError,
)
from videomaker.providers.image.cloudflare import (
    CLOUDFLARE_IMAGE_MODEL,
    COMPOSITION_GUIDANCE,
    FLUX_STEPS,
    IMAGE_SIZE,
    NEURONS_PER_IMAGE,
    CloudflareImageProvider,
)
from videomaker.providers.ratelimit import SOFT_BUDGETS, QuotaTracker
from videomaker.providers.stock.pexels import (
    MIN_PHOTO_SIDE,
    MIN_VIDEO_SIDE,
    SCENE_GAP_S,
    PexelsProvider,
)

JPEG_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "sample_photo.jpg"


# --------------------------------------------------------------------------- helpers


class FakeClock:
    """Frozen clock so quota windows never drift mid-test."""

    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _settings(**overrides) -> Settings:
    base = {
        "pexels_api_key": "pexels-key",
        "cloudflare_account_id": "acct-123",
        "cloudflare_api_token": "cf-token",
    }
    return Settings(**(base | overrides))


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def _quota(tmp_path: Path) -> QuotaTracker:
    return QuotaTracker(tmp_path / "quota.json", clock=FakeClock())


def _pexels(tmp_path: Path, handler, *, settings: Settings | None = None, **kwargs):
    return PexelsProvider(
        settings or _settings(),
        client=_client(handler),
        cache=ResponseCache(tmp_path / "responses", ttl_days=7),
        quota=_quota(tmp_path),
        **kwargs,
    )


def _video(vid: int, duration: int, files, name: str = "Jane Doe") -> dict:
    return {
        "id": vid,
        "url": f"https://www.pexels.com/video/{vid}/",
        "image": f"https://images.pexels.com/videos/{vid}/thumb.jpg",
        "duration": duration,
        "user": {"name": name},
        "video_files": [
            {"link": f"https://player.pexels.com/{vid}-{h}.mp4", "width": w, "height": h}
            for w, h in files
        ],
    }


def _photo(pid: int, width: int, height: int, photographer: str = "Ansel Example") -> dict:
    return {
        "id": pid,
        "url": f"https://www.pexels.com/photo/{pid}/",
        "width": width,
        "height": height,
        "photographer": photographer,
        "src": {
            "original": f"https://images.pexels.com/photos/{pid}/original.jpg",
            "large2x": f"https://images.pexels.com/photos/{pid}/large2x.jpg",
            "medium": f"https://images.pexels.com/photos/{pid}/medium.jpg",
        },
    }


HD = [(1280, 720), (1920, 1080)]
SD_ONLY = [(640, 360), (1280, 720)]


def _json_handler(body: dict, calls: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        return httpx.Response(200, json=body)

    return handler


# ----------------------------------------------------------------------- pexels: video


def test_video_shorter_than_the_scene_is_rejected(tmp_path):
    body = {
        "videos": [
            _video(1, duration=3, files=HD),  # 3s < 5.0 + gap
            _video(2, duration=8, files=HD),
        ]
    }
    provider = _pexels(tmp_path, _json_handler(body))
    results = provider.search(query="ssd", kind=VisualKind.STOCK_VIDEO, min_duration_s=5.0)
    assert [r.source_id for r in results] == ["2"]


def test_video_exactly_on_the_gap_boundary_is_kept(tmp_path):
    body = {"videos": [_video(7, duration=6, files=HD)]}
    provider = _pexels(tmp_path, _json_handler(body))
    results = provider.search(
        query="ssd", kind=VisualKind.STOCK_VIDEO, min_duration_s=6.0 - SCENE_GAP_S
    )
    assert [r.source_id for r in results] == ["7"]


def test_sub_1080p_video_is_rejected(tmp_path):
    body = {"videos": [_video(1, duration=20, files=SD_ONLY), _video(2, duration=20, files=HD)]}
    provider = _pexels(tmp_path, _json_handler(body))
    results = provider.search(query="ssd", kind=VisualKind.STOCK_VIDEO)
    assert [r.source_id for r in results] == ["2"]
    assert min(results[0].width, results[0].height) >= MIN_VIDEO_SIDE


def test_video_picks_the_cheapest_file_that_clears_the_bar(tmp_path):
    body = {"videos": [_video(1, duration=20, files=[(3840, 2160), (1920, 1080), (1280, 720)])]}
    provider = _pexels(tmp_path, _json_handler(body))
    (result,) = provider.search(query="ssd", kind=VisualKind.STOCK_VIDEO)
    assert (result.width, result.height) == (1920, 1080)
    assert result.download_url.endswith("1-1080.mp4")
    assert result.duration_s == 20.0


def test_video_attribution_is_captured_from_user_name(tmp_path):
    body = {"videos": [_video(1, duration=20, files=HD, name="Kelly Stock")]}
    provider = _pexels(tmp_path, _json_handler(body))
    (result,) = provider.search(query="ssd", kind=VisualKind.STOCK_VIDEO)
    assert "Kelly Stock" in result.attribution
    assert result.license


# ----------------------------------------------------------------------- pexels: photo


def test_photos_below_min_side_are_rejected(tmp_path):
    body = {
        "photos": [
            _photo(1, 4000, 1200),  # min side 1200
            _photo(2, 1200, 4000),  # min side 1200
            _photo(3, 2400, 1600),  # min side 1600 — exactly on the bar
        ]
    }
    provider = _pexels(tmp_path, _json_handler(body))
    results = provider.search(query="ssd", kind=VisualKind.STOCK_PHOTO)
    assert [r.source_id for r in results] == ["3"]
    assert min(results[0].width, results[0].height) >= MIN_PHOTO_SIDE
    assert results[0].duration_s is None


def test_photo_attribution_is_captured_from_photographer(tmp_path):
    body = {"photos": [_photo(3, 2400, 1600, photographer="Ada Shutter")]}
    provider = _pexels(tmp_path, _json_handler(body))
    (result,) = provider.search(query="ssd", kind=VisualKind.STOCK_PHOTO)
    assert "Ada Shutter" in result.attribution


def test_photo_search_hits_the_photo_endpoint(tmp_path):
    calls: list[httpx.Request] = []
    provider = _pexels(tmp_path, _json_handler({"photos": []}, calls))
    provider.search(query="ssd", kind=VisualKind.STOCK_PHOTO)
    assert calls[0].url.path == "/v1/search"
    assert calls[0].headers["Authorization"] == "pexels-key"


def test_video_search_hits_the_video_endpoint(tmp_path):
    calls: list[httpx.Request] = []
    provider = _pexels(tmp_path, _json_handler({"videos": []}, calls))
    provider.search(query="ssd", kind=VisualKind.AUTO, orientation="landscape")
    assert calls[0].url.path == "/videos/search"
    assert calls[0].url.params["query"] == "ssd"
    assert calls[0].url.params["orientation"] == "landscape"


# --------------------------------------------------------------- pexels: ranking, cache


def test_search_returns_per_page_results_in_rank_order(tmp_path):
    body = {"videos": [_video(i, duration=20, files=HD) for i in range(1, 7)]}
    calls: list[httpx.Request] = []
    provider = _pexels(tmp_path, _json_handler(body, calls))
    results = provider.search(query="ssd", kind=VisualKind.STOCK_VIDEO, per_page=4)
    assert [r.source_id for r in results] == ["1", "2", "3", "4"]
    # We over-fetch so that filtering does not hand back an empty page.
    assert int(calls[0].url.params["per_page"]) >= 4


def test_second_identical_search_makes_zero_http_requests(tmp_path):
    body = {"videos": [_video(1, duration=20, files=HD)]}
    calls: list[httpx.Request] = []
    provider = _pexels(tmp_path, _json_handler(body, calls))

    first = provider.search(query="ssd", kind=VisualKind.STOCK_VIDEO)
    assert len(calls) == 1
    spent = provider.quota.remaining("pexels", SOFT_BUDGETS["pexels"])["per_hour"]

    second = provider.search(query="ssd", kind=VisualKind.STOCK_VIDEO)
    assert len(calls) == 1, "a cached search must issue no HTTP request"
    assert [r.model_dump() for r in second] == [r.model_dump() for r in first]
    # A cache hit must not consume the hourly budget.
    assert provider.quota.remaining("pexels", SOFT_BUDGETS["pexels"])["per_hour"] == spent


def test_a_different_query_is_a_cache_miss(tmp_path):
    calls: list[httpx.Request] = []
    provider = _pexels(tmp_path, _json_handler({"videos": []}, calls))
    provider.search(query="ssd", kind=VisualKind.STOCK_VIDEO)
    provider.search(query="hdd", kind=VisualKind.STOCK_VIDEO)
    assert len(calls) == 2


# ------------------------------------------------------------------- pexels: failures


def test_429_becomes_transient_error_with_retry_after(tmp_path):
    def handler(request):
        return httpx.Response(429, headers={"Retry-After": "31"}, json={})

    provider = _pexels(tmp_path, handler)
    with pytest.raises(TransientError) as exc:
        provider.search(query="ssd", kind=VisualKind.STOCK_VIDEO)
    assert exc.value.retry_after_s == 31


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failures_are_config_errors(tmp_path, status):
    provider = _pexels(tmp_path, lambda request: httpx.Response(status, json={}))
    with pytest.raises(ProviderConfigError):
        provider.search(query="ssd", kind=VisualKind.STOCK_VIDEO)


def test_server_error_is_transient(tmp_path):
    provider = _pexels(tmp_path, lambda request: httpx.Response(503, text="nope"))
    with pytest.raises(TransientError):
        provider.search(query="ssd", kind=VisualKind.STOCK_VIDEO)


def test_missing_api_key_fails_fast_without_http(tmp_path):
    def handler(request):
        raise AssertionError("no HTTP call may be made without an API key")

    provider = _pexels(tmp_path, handler, settings=_settings(pexels_api_key=""))
    with pytest.raises(ProviderConfigError):
        provider.search(query="ssd", kind=VisualKind.STOCK_VIDEO)


def test_exhausted_hourly_budget_raises_before_any_http(tmp_path):
    def handler(request):
        raise AssertionError("the budget check must happen before the HTTP call")

    provider = _pexels(tmp_path, handler)
    provider.quota.record("pexels", SOFT_BUDGETS["pexels"].per_hour)
    with pytest.raises(QuotaExceeded):
        provider.search(query="ssd", kind=VisualKind.STOCK_VIDEO)


# ------------------------------------------------------------------- pexels: download


def test_download_streams_and_returns_a_project_relative_asset_ref(tmp_path):
    project_dir = tmp_path / "workspace" / "proj-1"
    (project_dir / "assets").mkdir(parents=True)
    (project_dir / "project.json").write_text("{}")

    search_body = {"videos": [_video(1, duration=20, files=HD, name="Kelly Stock")]}
    payload = b"fake-mp4-bytes"

    def handler(request):
        if request.url.host == "api.pexels.com":
            return httpx.Response(200, json=search_body)
        return httpx.Response(200, content=payload)

    provider = _pexels(tmp_path, handler)
    (result,) = provider.search(query="ssd", kind=VisualKind.STOCK_VIDEO)

    out_path = project_dir / "assets" / "scene1.mp4"
    ref = provider.download(result, out_path)

    assert out_path.read_bytes() == payload
    assert not out_path.with_suffix(".mp4.part").exists()
    assert ref.local_path == "assets/scene1.mp4"
    assert not Path(ref.local_path).is_absolute()
    assert ref.provider == "pexels"
    assert ref.source_id == "1"
    assert "Kelly Stock" in ref.attribution
    assert (ref.width, ref.height) == (1920, 1080)
    assert ref.duration_s == 20.0
    # Carried through, not discarded: the storyboard draws un-downloaded
    # alternatives from this, and Task 13's re-rank reads it too.
    assert result.preview_url
    assert ref.preview_url == result.preview_url


# ----------------------------------------------------------------------- cloudflare


def _flux_body() -> dict:
    return {
        "success": True,
        "result": {"image": base64.b64encode(JPEG_FIXTURE.read_bytes()).decode()},
    }


def _cloudflare(tmp_path: Path, handler, *, settings: Settings | None = None, quota=None):
    return CloudflareImageProvider(
        settings or _settings(),
        client=_client(handler),
        quota=quota if quota is not None else _quota(tmp_path),
    )


def test_flux_success_writes_a_real_jpeg_and_records_58_neurons(tmp_path):
    calls: list[httpx.Request] = []
    provider = _cloudflare(tmp_path, _json_handler(_flux_body(), calls))

    out_path = tmp_path / "assets" / "scene1.jpg"
    ref = provider.generate_image(prompt="an ssd chip", out_path=out_path, aspect=Aspect.WIDE)

    data = out_path.read_bytes()
    assert data[:3] == b"\xff\xd8\xff", "must be a real JPEG"
    assert data == JPEG_FIXTURE.read_bytes()
    assert not out_path.with_suffix(".jpg.part").exists()

    assert (ref.width, ref.height) == (IMAGE_SIZE, IMAGE_SIZE)
    assert ref.provider == "cloudflare"
    assert ref.local_path == "scene1.jpg"  # relative: no project.json above tmp_path
    # A generated image has no remote thumbnail to point at — the file on disk is
    # the only copy that exists — so the field stays empty rather than guessing.
    assert ref.preview_url == ""

    budget = SOFT_BUDGETS["cloudflare"]
    assert provider.quota.remaining("cloudflare", budget)["per_day"] == (
        budget.per_day - NEURONS_PER_IMAGE
    )


def test_flux_request_pins_the_model_steps_and_composition_guidance(tmp_path):
    calls: list[httpx.Request] = []
    provider = _cloudflare(tmp_path, _json_handler(_flux_body(), calls))
    provider.generate_image(prompt="an ssd chip", out_path=tmp_path / "a.jpg", aspect=Aspect.WIDE)

    (request,) = calls
    assert request.url.path == f"/client/v4/accounts/acct-123/ai/run/{CLOUDFLARE_IMAGE_MODEL}"
    assert request.headers["Authorization"] == "Bearer cf-token"
    body = json.loads(request.content)
    assert body["steps"] == FLUX_STEPS
    assert body["prompt"].startswith("an ssd chip")
    # M0 finding 5: Flux returns a 1024x1024 square, so ask for a croppable composition.
    assert COMPOSITION_GUIDANCE in body["prompt"]


@pytest.mark.parametrize("status", [401, 403])
def test_flux_auth_failure_is_a_config_error(tmp_path, status):
    provider = _cloudflare(tmp_path, lambda request: httpx.Response(status, json={}))
    with pytest.raises(ProviderConfigError):
        provider.generate_image(prompt="p", out_path=tmp_path / "a.jpg", aspect=Aspect.WIDE)


def test_flux_429_is_transient(tmp_path):
    provider = _cloudflare(
        tmp_path, lambda request: httpx.Response(429, headers={"Retry-After": "9"}, json={})
    )
    with pytest.raises(TransientError) as exc:
        provider.generate_image(prompt="p", out_path=tmp_path / "a.jpg", aspect=Aspect.WIDE)
    assert exc.value.retry_after_s == 9


def test_exhausted_neuron_budget_raises_before_any_http(tmp_path):
    def handler(request):
        raise AssertionError("Workers AI auto-bills past the free cap: check the budget first")

    quota = _quota(tmp_path)
    # Leave less headroom than one image costs; the call must never be attempted.
    quota.record("cloudflare", SOFT_BUDGETS["cloudflare"].per_day - NEURONS_PER_IMAGE + 1)
    provider = _cloudflare(tmp_path, handler, quota=quota)

    out_path = tmp_path / "a.jpg"
    with pytest.raises(QuotaExceeded):
        provider.generate_image(prompt="p", out_path=out_path, aspect=Aspect.WIDE)
    assert not out_path.exists()


def test_missing_cloudflare_credentials_fail_fast_without_http(tmp_path):
    def handler(request):
        raise AssertionError("no HTTP call may be made without credentials")

    provider = _cloudflare(
        tmp_path, handler, settings=_settings(cloudflare_api_token="", cloudflare_account_id="")
    )
    with pytest.raises(ProviderConfigError):
        provider.generate_image(prompt="p", out_path=tmp_path / "a.jpg", aspect=Aspect.WIDE)
