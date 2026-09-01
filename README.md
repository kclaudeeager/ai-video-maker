# AI Video Maker

Local-first, human-in-the-loop AI video studio. Turns a topic into a finished
long-form YouTube video **and** a 9:16 Short/Reel from the same project —
using only free/open models (Kokoro TTS, faster-whisper) and free API tiers
(Groq/Gemini scripts, Pexels footage, Cloudflare Workers AI images).

**Status: pre-alpha.** M0 (scaffold + environment doctor) is done; the video
pipeline lands in M1+. See `docs/superpowers/specs/` for the full design.

## Why another AI video tool?

Platforms now demonetize mass-produced templated AI content. This tool is the
opposite of a one-click bot: the pipeline pauses at three review gates
(script → storyboard → preview) so every video is individually curated.
Intended for original creator content — not spam farms or misinformation.

## Quickstart (Linux, Debian/Ubuntu-based)

```bash
sudo apt install -y ffmpeg   # distro builds include libass (captions)
curl -LsSf https://astral.sh/uv/install.sh | sh   # if uv isn't installed
git clone <repo-url> && cd ai-video-maker
uv sync --extra ml
cp .env.example .env         # fill in the free API keys (links inside)
uv run videomaker setup      # downloads models (~500 MB), runs smoke tests
uv run videomaker doctor     # everything should be OK/WARN, no FAIL
```

macOS users (including Intel Macs): see `docs/macos-intel-notes.md`.

## Running it on a server

`docker compose up --build` self-hosts it; `render.yaml` deploys it to Render as
a Blueprint. Both need `LONGHAND_PASSWORD` set — the server **refuses to start**
on a public address without one, because everything behind the UI spends your
provider quota and starts encodes on the host.

Read [`docs/deploying.md`](docs/deploying.md) first. The short version: the
image carries ~480 MB of model weights and needs **661 MB of RAM before it
renders anything**, so a 512 MB free tier cannot run it. Standard-sized
instances or your own machine behind a tunnel.

## Music and sound effects

**The project ships no audio files, ever.** Content ID issues false claims against
Creative Commons music routinely, and a claim on a monetised video is the outcome
this project exists to avoid — shipping nothing means it can never be the source
of one. Your library is your own: drop tracks into `assets/music/<mood>/` and
effects into `assets/sfx/<role>/`, and record each one's licence in
`assets/music/library.yaml` so video descriptions can credit them.

```bash
uv run videomaker music scan   # ffprobe the library, cache the durations
uv run videomaker music list   # print it, flagging anything uncredited
```

An empty library is **not an error** — the pipeline renders narration only. See
`assets/music/README.md` for where to get licence-clear audio (and for why you must
never rip audio from a YouTube video), and `docs/audio-design.md` for the design.

## License

AGPL-3.0-only. The project name is reserved by the maintainer; forks should
use their own name. Contributions require DCO sign-off (`git commit -s`).
