# Third-party notices

- **Kokoro-82M** TTS model — Apache-2.0 (hexgrad/Kokoro-82M); ONNX runtime
  packaging via kokoro-onnx (MIT).
- **faster-whisper** — MIT; Whisper models by OpenAI (MIT).
- **FFmpeg** — LGPL/GPL, invoked as a subprocess (not linked).
- **Pexels / Pixabay** media: free licenses including commercial use;
  attribution is appreciated and auto-generated per video (M5).
- **Groq, Google Gemini, Cloudflare Workers AI**: bring-your-own-key;
  usage is subject to each provider's terms.
- Bundled fonts (added in M3) are SIL OFL licensed; see assets/fonts/OFL.txt.
- **htmx** 2.0.4 — BSD-2-Clause (bigskysoftware/htmx). Vendored, not fetched
  from a CDN, so the review UI works offline and makes no third-party request:
  `src/videomaker/web/static/vendor/htmx.min.js`, 50917 bytes,
  SHA-256 `e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447`.
  Re-verify after any update:
  `sha256sum src/videomaker/web/static/vendor/htmx.min.js`.
