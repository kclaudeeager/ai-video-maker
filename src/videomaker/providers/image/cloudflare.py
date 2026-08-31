"""Cloudflare Workers AI text-to-image, pinned to Flux-1-schnell.

Two M0 findings shape this module, and both are load-bearing:

* **Finding 6 — the budget check must precede the HTTP call.** Workers AI does not
  fail once the free neuron allowance is spent; it *auto-bills*. Nothing downstream
  can undo that, so `QuotaTracker` is the only brake, and it is applied before the
  request is built. The model is pinned for the same reason: at ~58 neurons per
  1024x1024 image (4 steps) the free tier is ~170 images/day, whereas the Leonardo
  models cost 530-636 neurons *per tile* — two orders of magnitude more.
* **Finding 5 — Flux returns 1024x1024 squares.** Cropping one to 16:9 throws away
  about a third of the frame, so `COMPOSITION_GUIDANCE` is appended to every prompt:
  a centred subject with margins survives that crop, an edge-to-edge one does not.
  `aspect` therefore steers the *prompt*, never the request; the visuals stage does
  the actual crop.
"""

import base64
import binascii
import os
from pathlib import Path

import httpx

from videomaker.cache import hash_inputs
from videomaker.config import Settings
from videomaker.models import Aspect, AssetRef
from videomaker.providers import register
from videomaker.providers.assets import project_relative
from videomaker.providers.base import ImageProvider
from videomaker.providers.errors import (
    ProviderConfigError,
    ProviderResponseError,
    QuotaExceeded,
    TransientError,
)
from videomaker.providers.ratelimit import DEFAULT_QUOTA_PATH, SOFT_BUDGETS, QuotaTracker

PROVIDER_NAME = "cloudflare"

API_ROOT = "https://api.cloudflare.com/client/v4"
# M0 finding 6: pinned. Changing this without re-measuring NEURONS_PER_IMAGE would
# silently change what a render costs.
CLOUDFLARE_IMAGE_MODEL = "@cf/black-forest-labs/flux-1-schnell"
FLUX_STEPS = 4
NEURONS_PER_IMAGE = 58
IMAGE_SIZE = 1024

# M0 finding 5: the square has to survive a 16:9 crop.
COMPOSITION_GUIDANCE = "centred subject, generous headroom and margins, no text"

REQUEST_TIMEOUT_S = 120.0
JPEG_MAGIC = b"\xff\xd8\xff"

_AUTH_STATUSES = frozenset({401, 403})




def _retry_after_s(response: httpx.Response) -> float | None:
    try:
        return float(response.headers["Retry-After"])
    except (KeyError, TypeError, ValueError):
        return None


def _compose_prompt(prompt: str, negative_prompt: str) -> str:
    parts = [prompt.strip(), COMPOSITION_GUIDANCE]
    if negative_prompt.strip():
        # flux-1-schnell takes no `negative_prompt` field, so fold it into the text
        # rather than sending a parameter the endpoint would reject.
        parts.append(f"avoid: {negative_prompt.strip()}")
    return ". ".join(part for part in parts if part)


@register("image", PROVIDER_NAME)
class CloudflareImageProvider(ImageProvider):
    """Flux-1-schnell on Workers AI, policed by a neuron budget."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.Client | None = None,
        quota: QuotaTracker | None = None,
    ) -> None:
        self.settings = settings
        self._client = client
        self._owns_client = client is None
        self.quota = quota or QuotaTracker(DEFAULT_QUOTA_PATH)

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(follow_redirects=True, timeout=REQUEST_TIMEOUT_S)
        return self._client

    def _credentials(self) -> tuple[str, str]:
        account_id = self.settings.cloudflare_account_id.strip()
        token = self.settings.cloudflare_api_token.strip()
        if not account_id or not token:
            raise ProviderConfigError(
                "CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN must both be set to use "
                f"{CLOUDFLARE_IMAGE_MODEL}"
            )
        return account_id, token

    def _check_budget(self) -> None:
        """Refuse the call unless a whole image fits in the remaining allowance.

        `QuotaTracker.check` only asks whether the budget is *already* spent; one
        image costs 58 neurons, so headroom is compared against the real price.
        """
        budget = SOFT_BUDGETS[PROVIDER_NAME]
        self.quota.check(PROVIDER_NAME, budget)
        for axis, left in self.quota.remaining(PROVIDER_NAME, budget).items():
            if left is not None and left < NEURONS_PER_IMAGE:
                raise QuotaExceeded(
                    f"cloudflare soft budget has {left} neurons left on {axis}, "
                    f"one image costs {NEURONS_PER_IMAGE}; Workers AI would bill past the free cap"
                )

    def generate_image(
        self,
        *,
        prompt: str,
        out_path: Path,
        aspect: Aspect,
        negative_prompt: str = "",
        seed: int | None = None,
    ) -> AssetRef:
        account_id, token = self._credentials()
        # Before the request, always (M0 finding 6).
        self._check_budget()

        full_prompt = _compose_prompt(prompt, negative_prompt)
        body: dict[str, object] = {"prompt": full_prompt, "steps": FLUX_STEPS}
        if seed is not None:
            body["seed"] = int(seed)

        url = f"{API_ROOT}/accounts/{account_id}/ai/run/{CLOUDFLARE_IMAGE_MODEL}"
        try:
            response = self.client.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {token}"},
                timeout=REQUEST_TIMEOUT_S,
            )
        except httpx.RequestError as exc:
            raise TransientError(f"cloudflare request failed: {exc}") from exc

        if response.status_code in _AUTH_STATUSES:
            raise ProviderConfigError(
                f"cloudflare rejected the credentials (HTTP {response.status_code}); check "
                "CLOUDFLARE_API_TOKEN has the Workers AI scope and CLOUDFLARE_ACCOUNT_ID is right"
            )
        if response.status_code == 429:
            raise TransientError(
                "cloudflare rate limit reached", retry_after_s=_retry_after_s(response)
            )
        if response.status_code >= 500:
            raise TransientError(f"cloudflare server error (HTTP {response.status_code})")
        if response.status_code >= 400:
            raise ProviderResponseError(
                f"cloudflare returned HTTP {response.status_code}: {response.text[:200]}"
            )

        image = self._decode_image(response)
        # Booked only once the neurons are genuinely spent.
        self.quota.record(PROVIDER_NAME, NEURONS_PER_IMAGE)
        self.quota.save()

        path = Path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(image)
        os.replace(tmp, path)

        source_id = hash_inputs(
            model=CLOUDFLARE_IMAGE_MODEL,
            prompt=full_prompt,
            aspect=str(aspect),
            seed=seed,
        )
        return AssetRef(
            provider=PROVIDER_NAME,
            source_id=source_id,
            # Deliberately not the request URL: that embeds the account id, and
            # `project.json` is a shareable artefact.
            source_url=f"cf-workers-ai:{CLOUDFLARE_IMAGE_MODEL}",
            local_path=project_relative(path),
            # Generated here, so the file on disk is the only copy of it that exists;
            # there is no provider-hosted thumbnail to point the storyboard at.
            preview_url="",
            # Flux always returns a square (M0 finding 5); `aspect` is the caller's
            # crop target, not the image's shape.
            width=IMAGE_SIZE,
            height=IMAGE_SIZE,
            duration_s=None,
            attribution="",
            license=f"Generated with {CLOUDFLARE_IMAGE_MODEL} on Cloudflare Workers AI",
        )

    @staticmethod
    def _decode_image(response: httpx.Response) -> bytes:
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderResponseError("cloudflare returned a non-JSON body") from exc
        result = payload.get("result") if isinstance(payload, dict) else None
        encoded = result.get("image") if isinstance(result, dict) else None
        if not isinstance(encoded, str) or not encoded:
            raise ProviderResponseError("cloudflare response carried no result.image")
        try:
            image = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ProviderResponseError("cloudflare result.image was not valid base64") from exc
        if not image.startswith(JPEG_MAGIC):
            raise ProviderResponseError("cloudflare result.image was not a JPEG")
        return image

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None
