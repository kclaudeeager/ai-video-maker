# Longhand, containerised. M6.
#
# Two decisions shape this file and both cost image size on purpose.
#
# **FFmpeg comes from apt, not from a static build.** Debian's ffmpeg is compiled
# with libass, which the `subtitles` filter needs to burn captions in — M0's very
# first finding was a stack where it was missing and every render failed at the
# last step, so the build asserts the filter is present rather than trusting it.
#
# **The model weights are baked in, not downloaded at boot.** Kokoro (311 MB) plus
# its voices (27 MB) plus faster-whisper base (142 MB) is ~480 MB. Fetching that
# on first request would put a five-minute stall in front of a cold start on a
# platform that spins containers down when idle, and would break entirely on a
# host with no egress to GitHub. Baking them makes the image large and the boot
# honest. There is no cloud fallback for either: `provider_chains` ships
# `tts: [kokoro]` and `stt: [fasterwhisper]`, so a container without these weights
# cannot voice or align a single scene.

# ---------------------------------------------------------------- build stage
FROM python:3.12-slim-bookworm AS build

# uv resolves and installs from the committed lockfile, so the image gets the
# exact versions CI tested rather than whatever resolves on build day.
COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies before source: this layer is the slow one (onnxruntime and
# ctranslate2 are large wheels) and it must not be invalidated by an edit to a
# template. Only the lockfile and manifest are copied here.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --extra ml

COPY src/ ./src/
COPY templates/ ./templates/
COPY README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --extra ml


# --------------------------------------------------------------- model stage
# Separate so the ~480 MB of weights is one cached layer that survives every
# code change. It is by far the most expensive thing to rebuild.
FROM python:3.12-slim-bookworm AS models

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ARG KOKORO_RELEASE=https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0
WORKDIR /models
RUN curl -fsSL -o kokoro-v1.0.onnx "${KOKORO_RELEASE}/kokoro-v1.0.onnx" \
    && curl -fsSL -o voices-v1.0.bin "${KOKORO_RELEASE}/voices-v1.0.bin"

# faster-whisper pulls its own weights through huggingface_hub, so the only way
# to pre-seed them is to ask the library for the model once and keep its cache.
COPY --from=build /app/.venv /venv
ENV PATH="/venv/bin:$PATH"
RUN python -c "\
from faster_whisper import WhisperModel; \
WhisperModel('base', device='cpu', compute_type='int8', download_root='/models/whisper')"


# -------------------------------------------------------------- runtime stage
FROM python:3.12-slim-bookworm AS runtime

# ffmpeg brings libass; fonts-dejavu-core is the caption face `media/ass.py`
# names as its default, and libass silently substitutes something else if it is
# absent — which is a caption layout bug you only find by looking at a frame.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-dejavu-core \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Never root. The app writes only under /data and runs FFmpeg on untrusted-ish
# stock downloads; there is no reason for either to have more than one user's
# worth of authority.
RUN useradd --create-home --uid 10001 longhand

COPY --from=build --chown=longhand:longhand /app/.venv /app/.venv
COPY --from=build --chown=longhand:longhand /app/src /app/src
COPY --from=build --chown=longhand:longhand /app/templates /app/templates
COPY --from=models --chown=longhand:longhand /models /home/longhand/.cache/ai-video-maker/models

# WORKSPACE_DIR is read by pydantic-settings straight off the field name — there
# is no env prefix on `Settings`. It points into /data, where a persistent volume
# gets mounted; without one, every project is lost on restart. See
# docs/deploying.md. (Kept out of the continuation above: a comment inside a
# line-continued ENV is a parser corner nobody should have to think about.)
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WORKSPACE_DIR=/data/workspace \
    HF_HOME=/home/longhand/.cache/huggingface

# Created as root, handed to the app user. A volume mounted over it at runtime
# inherits this ownership on Docker and is chowned by the platform on Render.
RUN mkdir -p /data/workspace && chown -R longhand:longhand /data

WORKDIR /app
USER longhand

# Prove at *build* time that this ffmpeg can burn captions. M0's first finding
# was a stack whose ffmpeg lacked libass, where every render died at the last
# step after twenty minutes of work. Asserted directly rather than through
# `videomaker doctor`, which also checks API keys that are correctly absent from
# a build — a check that always passes protects nothing.
RUN ffmpeg -hide_banner -filters 2>/dev/null | grep -qw subtitles \
    || (echo 'FATAL: this ffmpeg has no subtitles filter (built without libass)' && exit 1)

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

# 0.0.0.0 because the container's own loopback is not reachable from outside it.
# `serve` refuses that bind unless LONGHAND_PASSWORD is set, which is the whole
# point: an image that is trivially deployable must not be trivially open.
CMD ["sh", "-c", "videomaker serve --host 0.0.0.0 --port ${PORT:-8000}"]
