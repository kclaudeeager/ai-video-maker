"""Gemini Flash as a visual second opinion on stock candidates.

Item 4 of `docs/visual-search-design.md`. `pipeline.ranking` reads tags, duration
and pixel counts; none of those is the picture, so it cannot tell a clip *about*
NAND cells from a warehouse shelf whose stencilled text happened to match. Gemini
Flash's free tier accepts images, so asking it is available at $0 — which is not
the same as free of consequence, and everything below is shaped by that.

**The budget, honestly.** The design doc budgets one request per image: 10 scenes
x 4 candidates = 40 images a video, and Gemini's *published* free tier is 1,500
requests a day, so ~37 videos. This project's own ceiling is tighter than that.
`SOFT_BUDGETS["gemini"]` is **240 a day**, shaded under the 250/day of the
`generateContent` free tier for the flash models we prefer, and the script stage's
LLM fallback spends from that same allowance. Forty requests a video would be a
sixth of the day's budget for one render, and would compete with the calls that
actually write the script.

So this batches: **one request per scene**, with every candidate thumbnail inline
and numbered. That costs a gated scene one unit instead of four, and it is also
the better question — a re-rank is a comparison, and the model can only compare
what it is shown together.

The rest is inherited from `llm/gemini.py` on purpose: the `x-goog-api-key`
header, the model resolution that refuses to accept an arbitrary id (M0 finding
1), the error taxonomy, the response cache and the quota tracker are the same
account and the same wire format. What is added here is fetching the thumbnails
(inline bytes; the Gemini `fileData` part only accepts Files API URIs, not
arbitrary URLs) and parsing a score per image.
"""

import base64
import json
from typing import Any

import httpx

from videomaker.cache import hash_inputs
from videomaker.providers import register
from videomaker.providers.base import VisionProvider
from videomaker.providers.errors import ProviderResponseError, TransientError
from videomaker.providers.llm.gemini import (
    GEMINI_API_VERSION,
    GEMINI_BASE_URL,
    GENERATE_CONTENT_METHOD,
    GeminiProvider,
)
from videomaker.providers.ratelimit import SOFT_BUDGETS

PROVIDER_NAME = "gemini"

#: Thumbnails, not footage: Pexels' `medium` rendition is tens of kilobytes. A cap
#: well above that still refuses to base64 a full-resolution photo into a request
#: body — which would be slow, and would spend a request on something the model
#: cannot use better than the small version.
MAX_THUMBNAIL_BYTES = 2_000_000
THUMBNAIL_TIMEOUT_S = 20.0

#: The answer is a short array of numbers. Anything longer is the model narrating.
MAX_SCORE_TOKENS = 256
#: A ranking should not change between two identical runs.
SCORE_TEMPERATURE = 0.0

VISION_SYSTEM = (
    "You rate stock footage thumbnails for one scene of a narrated explainer video. "
    "You are given the scene's narration, the search query that found the images, and "
    "the candidate thumbnails in order, numbered from 0. Score how well each image "
    "illustrates what the narration is actually about. Judge the subject, not the "
    "photography: an attractive picture of the wrong thing scores near 0, and so does "
    "a literal match on a word from the query that shows an unrelated subject. "
    "Answer with JSON only."
)


def _score_prompt(query: str, narration: str, count: int) -> str:
    return (
        f"Scene narration: {narration}\n"
        f"Search query used: {query}\n"
        f"There are {count} thumbnails, in order.\n"
        f'Reply with {{"scores": [...]}} — exactly {count} numbers between 0.0 and 1.0, '
        "in the same order as the thumbnails. No other keys, no commentary."
    )


@register("vision", PROVIDER_NAME)
class GeminiVisionProvider(GeminiProvider, VisionProvider):
    """Scores candidate thumbnails against a scene, one batched request per scene."""

    # ---------------------------------------------------------------- thumbnails

    def _thumbnail(self, url: str) -> tuple[str, bytes]:
        """One thumbnail as `(mime type, bytes)`.

        Failures here are `ProviderError`s like any other, because the caller's whole
        contract is that a re-rank it cannot complete falls back to the metadata
        order. A missing thumbnail is not a broken render.
        """
        try:
            response = self.client.get(
                url, follow_redirects=True, timeout=THUMBNAIL_TIMEOUT_S
            )
        except httpx.RequestError as exc:
            raise TransientError(f"could not fetch thumbnail {url}: {exc}") from exc
        if not response.is_success:
            raise TransientError(f"thumbnail {url} returned HTTP {response.status_code}")
        mime = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if not mime.startswith("image/"):
            raise ProviderResponseError(f"thumbnail {url} is {mime or 'untyped'}, not an image")
        content = response.content
        if len(content) > MAX_THUMBNAIL_BYTES:
            raise ProviderResponseError(
                f"thumbnail {url} is {len(content)} bytes, over the {MAX_THUMBNAIL_BYTES} cap"
            )
        return mime, content

    # -------------------------------------------------------------------- scoring

    def score_images(
        self,
        *,
        image_urls: list[str],
        query: str,
        narration: str,
    ) -> list[float]:
        """One score per URL, in order. See `VisionProvider` for the contract."""
        if not image_urls:
            return []
        self._require_api_key()
        model = self._resolve_model()
        key = hash_inputs(
            provider=self.provider_name,
            model=model,
            task="visual-rerank",
            urls=list(image_urls),
            query=query,
            narration=narration,
        )
        if self.cache is not None:
            cached = self._cached_scores(key, len(image_urls))
            if cached is not None:
                # A hit costs nothing, so the quota tracker is never touched.
                return cached

        if self.quota is not None:
            # Checked before the thumbnails are even fetched: a spent budget must
            # cost nothing, and downloading four images we can never send is a cost.
            self.quota.check(self.provider_name, SOFT_BUDGETS[self.provider_name])
        images = [self._thumbnail(url) for url in image_urls]
        try:
            body = self._generate(model=model, query=query, narration=narration, images=images)
        finally:
            # The request left the machine even if it came back 429 or 500, so book
            # it either way. `record` only mutates memory and `build_deps` makes a
            # fresh tracker per job, so without the `save` a *daily* budget could
            # never be enforced across invocations (M2 finding).
            if self.quota is not None:
                self.quota.record(self.provider_name)
                self.quota.save()

        scores = _parse_scores(self._first_text(body), len(image_urls))
        if self.cache is not None:
            self.cache.put(key, {"scores": scores})
        return scores

    def _cached_scores(self, key: str, count: int) -> list[float] | None:
        entry = self.cache.get(key) if self.cache is not None else None
        scores = entry.get("scores") if entry else None
        if not isinstance(scores, list) or len(scores) != count:
            return None
        if not all(isinstance(score, int | float) for score in scores):
            return None
        return [float(score) for score in scores]

    def _generate(
        self,
        *,
        model: str,
        query: str,
        narration: str,
        images: list[tuple[str, bytes]],
    ) -> dict[str, Any]:
        parts: list[dict[str, Any]] = [
            {"text": _score_prompt(query, narration, len(images))},
            *(
                {
                    "inline_data": {
                        "mime_type": mime,
                        "data": base64.b64encode(content).decode("ascii"),
                    }
                }
                for mime, content in images
            ),
        ]
        payload = {
            "contents": [{"role": "user", "parts": parts}],
            "systemInstruction": {"parts": [{"text": VISION_SYSTEM}]},
            "generationConfig": {
                "temperature": SCORE_TEMPERATURE,
                "maxOutputTokens": MAX_SCORE_TOKENS,
                "responseMimeType": "application/json",
            },
        }
        response = self.client.post(
            f"{GEMINI_BASE_URL}/{GEMINI_API_VERSION}/models/{model}:{GENERATE_CONTENT_METHOD}",
            headers=self._headers(),
            json=payload,
        )
        self._raise_for_status(response)
        return self._json(response)


def _parse_scores(text: str, count: int) -> list[float]:
    """`count` scores clamped to `0.0-1.0`, or a `ProviderResponseError`.

    Strict on length. A short or long array means the model lost track of which
    thumbnail was which, and a re-rank applied to the wrong candidate is worse than
    no re-rank at all — the caller has a perfectly good metadata order to fall back
    to.
    """
    try:
        body = json.loads(text)
    except ValueError as exc:
        raise ProviderResponseError(f"gemini vision returned non-JSON: {text[:200]}") from exc
    scores = body.get("scores") if isinstance(body, dict) else body
    if not isinstance(scores, list) or len(scores) != count:
        raise ProviderResponseError(
            f"gemini vision returned {len(scores) if isinstance(scores, list) else '?'} "
            f"scores for {count} thumbnails"
        )
    if not all(isinstance(score, int | float) and not isinstance(score, bool) for score in scores):
        raise ProviderResponseError(f"gemini vision returned non-numeric scores: {text[:200]}")
    return [max(0.0, min(1.0, float(score))) for score in scores]
