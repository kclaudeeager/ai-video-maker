# Third-party notices

- **Kokoro-82M** TTS model — Apache-2.0 (hexgrad/Kokoro-82M); ONNX runtime
  packaging via kokoro-onnx (MIT).
- **faster-whisper** — MIT; Whisper models by OpenAI (MIT).
- **FFmpeg** — LGPL/GPL, invoked as a subprocess (not linked).
- **Pexels / Pixabay** media: free licenses including commercial use;
  attribution is appreciated and auto-generated per video (M5).
- **Groq, Google Gemini, Cloudflare Workers AI**: bring-your-own-key;
  usage is subject to each provider's terms.
- **Space Grotesk** — SIL OFL 1.1 (Copyright 2017-2020 The Space Grotesk Project
  Authors, floriankarsten/space-grotesk). The UI's display face, and the face M3
  Task 15 draws thumbnail text with. Vendored, not fetched from a CDN, so the
  review UI works offline and makes no third-party request:
  `SpaceGrotesk-Regular.ttf`, 114428 bytes, SHA-256 `5ede28c4425f3fe4830c8f4754b39e9a87a93d0c3baa5e0a9924532aaa8a98bd`.
  `SpaceGrotesk-Medium.ttf`, 116664 bytes, SHA-256 `3183b7eb0b4241476360e2cd8e527868616cc16d3ce332c4376505989b772b44`.
  `SpaceGrotesk-Bold.ttf`, 116056 bytes, SHA-256 `7209bbb75fc0f5c546a5f5773b0db74ffc6abf04c2148c1105cace5765a96bdb`.
- **Charis SIL** — SIL OFL 1.1 with Reserved Font Names "Charis" and "SIL"
  (Copyright (c) 1997-2023 SIL International; basic character set similar to
  Bitstream Charter by Matthew Carter). The UI's text face — narration and prose.
  Shipped byte-for-byte as released: a subset would be a Modified Version, which
  may not keep the reserved name.
  `CharisSIL-Regular.woff2`, 293469 bytes, SHA-256 `9e15f73f23b1af26c1e8991fd673a0a20c4970b600a1ec1b53d8e7f61209c493`.
  `CharisSIL-Bold.woff2`, 292821 bytes, SHA-256 `e69f0ca63adec46b126084598a0a7ad35c03a8aee75c9537f3f7c42680fbc3dc`.
  Both families live in `src/videomaker/assets/fonts/` (inside the package, so
  the wheel carries them) with the full licence at `fonts/OFL.txt`. Re-verify
  after any update: `sha256sum src/videomaker/assets/fonts/*`.
- **htmx** 2.0.4 — BSD-2-Clause (bigskysoftware/htmx). Vendored, not fetched
  from a CDN, so the review UI works offline and makes no third-party request:
  `src/videomaker/web/static/vendor/htmx.min.js`, 50917 bytes,
  SHA-256 `e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447`.
  Re-verify after any update:
  `sha256sum src/videomaker/web/static/vendor/htmx.min.js`.
