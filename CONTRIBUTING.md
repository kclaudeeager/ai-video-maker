# Contributing

## Setup

Requires [uv](https://docs.astral.sh/uv/) and FFmpeg with libass
(`brew install ffmpeg` on macOS, `apt install ffmpeg` on Debian/Ubuntu).

```bash
uv sync --extra ml
uv run pytest        # unit tests (no API keys or models needed)
uv run ruff check .
```

## Rules

- **DCO**: sign off every commit (`git commit -s`). No CLA.
- **TDD**: new behavior arrives with a failing test first.
- **Niche templates** are YAML files in `templates/` — the easiest way to
  contribute, no Python needed (schema docs land in M4).
- **Providers** (LLM/TTS/STT/stock/image/upload) implement the ABCs in
  `src/videomaker/providers/base.py` (lands in M1). Free-tier-friendly
  providers are preferred; paid providers must be optional.
- Keep the $0-by-default promise: never make a paid service required.
