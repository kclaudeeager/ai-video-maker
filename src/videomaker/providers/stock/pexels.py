"""Pexels stock video and photo search.

Two endpoints, one shape: ``/videos/search`` returns ``videos[]`` (each with a
``video_files[]`` ladder of renditions), ``/v1/search`` returns ``photos[]`` with a
``src`` map. Both were exercised live in M0, so the field names below are measured,
not guessed.

Three rules drive everything here:

* **Filter hard.** A clip shorter than its scene cannot cover it, and anything under
  1080p would be upscaled at render time. Photos need a 1600 px short side so both
  the wide and the (M3) vertical crop stay inside real pixels.
* **Never pay twice.** Raw responses go through `ResponseCache` with a 7-day TTL,
  keyed on the *request*, not on the filters — so re-searching the same query for a
  slightly different scene length is free. A cache hit must not touch the quota.
* **Always attribute.** The Pexels licence requires crediting the creator, and M5's
  attribution block reads `AssetRef.attribution`, so it is populated at search time
  and carried through `download` rather than re-derived later.
"""

from pathlib import Path

import httpx

from videomaker.cache import ResponseCache, hash_inputs
from videomaker.config import Settings
from videomaker.downloads import download_file
from videomaker.models import AssetRef, StockResult, VisualKind
from videomaker.project import PROJECT_FILE
from videomaker.providers import register
from videomaker.providers.base import StockProvider
from videomaker.providers.errors import (
    ProviderConfigError,
    ProviderResponseError,
    TransientError,
)
from videomaker.providers.ratelimit import DEFAULT_QUOTA_PATH, SOFT_BUDGETS, QuotaTracker

PROVIDER_NAME = "pexels"

VIDEO_SEARCH_URL = "https://api.pexels.com/videos/search"
PHOTO_SEARCH_URL = "https://api.pexels.com/v1/search"

# Spec filters. `SCENE_GAP_S` is the breathing room between a scene's narration and
# the next cut: a clip has to cover the scene *and* that gap, or it will run dry.
MIN_VIDEO_SIDE = 1080
MIN_PHOTO_SIDE = 1600
SCENE_GAP_S = 0.5

# Filtering discards candidates, so ask for more than we intend to keep — otherwise a
# page of 720p clips spends a quota unit and returns nothing. 80 is the Pexels maximum.
OVERFETCH_FACTOR = 3
MAX_PER_PAGE = 80

CACHE_TTL_DAYS = 7
DEFAULT_CACHE_ROOT = Path.home() / ".cache" / "ai-video-maker" / "responses"

PEXELS_LICENSE = "Pexels License (https://www.pexels.com/license/)"

REQUEST_TIMEOUT_S = 30.0
DOWNLOAD_TIMEOUT_S = 120.0

_AUTH_STATUSES = frozenset({401, 403})


def _project_relative(out_path: Path) -> str:
    """`AssetRef.local_path` is always relative to the project folder.

    The project folder is the nearest ancestor holding a `project.json`. Outside a
    project (unit tests writing into a bare tmp dir) fall back to the bare filename,
    which is still a valid relative path.
    """
    path = Path(out_path).resolve()
    for parent in path.parents:
        if (parent / PROJECT_FILE).is_file():
            return path.relative_to(parent).as_posix()
    return path.name


def _wants_video(kind: VisualKind) -> bool:
    """AUTO means "whatever suits"; the visuals stage prefers footage, so default to it."""
    return kind is not VisualKind.STOCK_PHOTO and kind is not VisualKind.AI_IMAGE


def _retry_after_s(response: httpx.Response) -> float | None:
    try:
        return float(response.headers["Retry-After"])
    except (KeyError, TypeError, ValueError):
        return None


@register("stock", PROVIDER_NAME)
class PexelsProvider(StockProvider):
    """Stock search and download against the Pexels free tier (200 requests/hour)."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.Client | None = None,
        cache: ResponseCache | None = None,
        quota: QuotaTracker | None = None,
    ) -> None:
        self.settings = settings
        self._client = client
        self._owns_client = client is None
        self.cache = cache or ResponseCache(DEFAULT_CACHE_ROOT, ttl_days=CACHE_TTL_DAYS)
        self.quota = quota or QuotaTracker(DEFAULT_QUOTA_PATH)

    # ------------------------------------------------------------------ plumbing

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(follow_redirects=True, timeout=DOWNLOAD_TIMEOUT_S)
        return self._client

    def _api_key(self) -> str:
        key = self.settings.pexels_api_key.strip()
        if not key:
            raise ProviderConfigError(
                "PEXELS_API_KEY is not set; get a free key at https://www.pexels.com/api/"
            )
        return key

    def _get_json(self, url: str, params: dict[str, object]) -> dict:
        """One budgeted, error-mapped GET. Callers must have checked the cache first."""
        api_key = self._api_key()
        # Checked *before* the request: a spent budget must cost nothing.
        self.quota.check(PROVIDER_NAME, SOFT_BUDGETS[PROVIDER_NAME])
        try:
            response = self.client.get(
                url,
                params=params,
                headers={"Authorization": api_key},
                timeout=REQUEST_TIMEOUT_S,
            )
        except httpx.RequestError as exc:
            raise TransientError(f"pexels request failed: {exc}") from exc

        if response.status_code in _AUTH_STATUSES:
            raise ProviderConfigError(
                f"pexels rejected the API key (HTTP {response.status_code}); check PEXELS_API_KEY"
            )
        if response.status_code == 429:
            raise TransientError(
                "pexels rate limit reached", retry_after_s=_retry_after_s(response)
            )
        if response.status_code >= 500:
            raise TransientError(f"pexels server error (HTTP {response.status_code})")
        if response.status_code >= 400:
            raise ProviderResponseError(
                f"pexels returned HTTP {response.status_code}: {response.text[:200]}"
            )

        self.quota.record(PROVIDER_NAME, 1)
        self.quota.save()
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderResponseError("pexels returned a non-JSON body") from exc
        if not isinstance(payload, dict):
            raise ProviderResponseError(
                f"pexels returned {type(payload).__name__}, expected object"
            )
        return payload

    # -------------------------------------------------------------------- search

    def search(
        self,
        *,
        query: str,
        kind: VisualKind,
        min_duration_s: float = 0.0,
        orientation: str = "landscape",
        per_page: int = 4,
    ) -> list[StockResult]:
        video = _wants_video(kind)
        wanted = max(int(per_page), 1)
        fetch = min(wanted * OVERFETCH_FACTOR, MAX_PER_PAGE)
        url = VIDEO_SEARCH_URL if video else PHOTO_SEARCH_URL
        params: dict[str, object] = {
            "query": query,
            "per_page": fetch,
            "orientation": orientation,
        }
        # Keyed on the request only: `min_duration_s` filters locally, so scenes of
        # different lengths share one cached page instead of each buying their own.
        key = hash_inputs(provider=PROVIDER_NAME, url=url, **params)

        payload = self.cache.get(key)
        if payload is None:
            payload = self._get_json(url, params)
            self.cache.put(key, payload)

        parse = self._video_result if video else self._photo_result
        items = payload.get("videos" if video else "photos")
        if not isinstance(items, list):
            raise ProviderResponseError("pexels response had no result list")

        results: list[StockResult] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            result = parse(item, min_duration_s)
            if result is not None:
                results.append(result)
            if len(results) == wanted:
                break
        return results

    def _video_result(self, item: dict, min_duration_s: float) -> StockResult | None:
        duration_s = float(item.get("duration") or 0.0)
        # A clip must cover its scene *and* the gap before the next cut.
        if duration_s < min_duration_s + SCENE_GAP_S:
            return None

        candidates = [
            (int(f.get("width") or 0), int(f.get("height") or 0), str(f.get("link") or ""))
            for f in item.get("video_files") or []
            if isinstance(f, dict)
        ]
        usable = [(w, h, link) for w, h, link in candidates if link and min(w, h) >= MIN_VIDEO_SIDE]
        if not usable:
            return None
        # Cheapest rendition that still clears 1080p: bigger costs bandwidth, not quality.
        width, height, link = min(usable, key=lambda f: f[0] * f[1])

        user = item.get("user") or {}
        return StockResult(
            provider=PROVIDER_NAME,
            source_id=str(item.get("id", "")),
            source_url=str(item.get("url") or ""),
            preview_url=str(item.get("image") or ""),
            download_url=link,
            width=width,
            height=height,
            duration_s=duration_s,
            attribution=str(user.get("name") or "") if isinstance(user, dict) else "",
            license=PEXELS_LICENSE,
        )

    def _photo_result(self, item: dict, min_duration_s: float) -> StockResult | None:
        del min_duration_s  # photos are stills; duration is the caller's problem
        width = int(item.get("width") or 0)
        height = int(item.get("height") or 0)
        # Both the wide and the (M3) vertical crop must land inside real pixels.
        if min(width, height) < MIN_PHOTO_SIDE:
            return None

        src = item.get("src") or {}
        if not isinstance(src, dict):
            return None
        download_url = str(src.get("original") or src.get("large2x") or src.get("large") or "")
        if not download_url:
            return None

        return StockResult(
            provider=PROVIDER_NAME,
            source_id=str(item.get("id", "")),
            source_url=str(item.get("url") or ""),
            preview_url=str(src.get("medium") or src.get("large") or download_url),
            download_url=download_url,
            width=width,
            height=height,
            duration_s=None,
            attribution=str(item.get("photographer") or ""),
            license=PEXELS_LICENSE,
        )

    # ------------------------------------------------------------------ download

    def download(
        self,
        result: StockResult,
        out_path: Path,
        *,
        max_height: int = 1080,
    ) -> AssetRef:
        """Stream the chosen rendition to `out_path` (`.part` + `os.replace`).

        `max_height` is honoured at *search* time — `download_url` already points at
        the smallest rendition clearing `MIN_VIDEO_SIDE`, and a `StockResult` carries
        no ladder to re-pick from — so there is nothing left to choose here.
        """
        del max_height
        path = Path(out_path)
        try:
            download_file(result.download_url, path, client=self.client)
        except httpx.HTTPStatusError as exc:
            raise TransientError(
                f"pexels download failed (HTTP {exc.response.status_code}): {result.download_url}"
            ) from exc
        except httpx.RequestError as exc:
            raise TransientError(f"pexels download failed: {exc}") from exc

        return AssetRef(
            provider=PROVIDER_NAME,
            source_id=result.source_id,
            source_url=result.source_url,
            local_path=_project_relative(path),
            width=result.width,
            height=result.height,
            duration_s=result.duration_s,
            attribution=result.attribution,
            license=result.license,
        )

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None
