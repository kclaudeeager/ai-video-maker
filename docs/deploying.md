# Deploying Longhand

Longhand is a local-first tool. Running it on a server is a supported thing to do
— that is what M6 is — but it is worth being clear about what changes when you
do, because the honest version is not "push it to a free tier and you are done".

## The one rule

**Nothing goes public without `LONGHAND_PASSWORD`.** Everything behind the UI
spends your provider quota, reads any file inside a project folder, and starts
multi-minute encodes on the host's CPU. `videomaker serve` **refuses to start**
on a non-loopback bind with no password set — it does not warn and serve anyway,
because a warning scrolls past and an open port does not.

Generate one that is worth having:

```bash
python -c 'import secrets; print(secrets.token_urlsafe(24))'
```

The username is `longhand`. It is HTTP Basic, so **use it over HTTPS only** —
Render and Cloudflare Tunnel both give you TLS for free; a bare VPS on port 80
sends the password in the clear.

## What it actually costs to run

This is the part that decides everything else, so it comes first.

| | free tier | what happens |
|---|---|---|
| **RAM** | 512 MB | **Measured in this image: 661 MB resident** with both models loaded and nothing else happening — 38 MB baseline, +201 MB for faster-whisper base, +422 MB for Kokoro. That is before FFmpeg, before serving a request, before any project data. It runs out of memory before rendering a frame. |
| **CPU** | 0.1 core | A two-minute video took **~70 s** of libx264 on one full desktop core (M3 measurement). A tenth of a core makes that a quarter of an hour. |
| **Disk** | none | Free tiers have no persistent disk. `workspace/` *is* the product state; without a volume every project vanishes on the next deploy. |
| **Idle** | spins down at 15 min | A cold start reloads 480 MB of weights. |

That 661 MB is not an estimate. Reproduce it against any built image —
`--network none` also proves the weights are really baked in rather than fetched
on first use:

```bash
docker run --rm --network none -e LONGHAND_PASSWORD=x longhand:latest python -c "
from videomaker.config import load_settings
s = load_settings()
rss = lambda: int(open('/proc/self/status').read().split('VmRSS:')[1].split()[0]) / 1024
print(f'baseline                 {rss():7.0f} MB')
from faster_whisper import WhisperModel
w = WhisperModel('base', device='cpu', compute_type='int8', download_root=str(s.models_dir/'whisper'))
print(f'+ whisper base (int8)    {rss():7.0f} MB')
from kokoro_onnx import Kokoro
k = Kokoro(str(s.models_dir/'kokoro-v1.0.onnx'), str(s.models_dir/'voices-v1.0.bin'))
print(f'+ kokoro (both resident) {rss():7.0f} MB')
"
```

There is no configuration that makes a 512 MB / 0.1 CPU box render video. The
models are not optional either: `provider_chains` ships `tts: [kokoro]` and
`stt: [fasterwhisper]` with **no cloud fallback**, so a container that cannot
load them cannot voice or align a single scene.

**The realistic floor is Render Standard — 2 GB, 1 CPU, ~$25/month — plus a
10 GB disk at $0.25/GB.** Call it **$27.50/month**. Everything below assumes
that; `render.yaml` encodes it.

If that is more than this is worth right now, the honest alternative is a
**Cloudflare Tunnel** pointed at the container on your own machine: free, TLS,
no open ports, and the renders run on hardware you have already paid for. The
password gate applies identically. See [Cloudflare Tunnel — the free
path](#cloudflare-tunnel--the-free-path) below.

## Render

1. Push this repo to GitHub (it already is).
2. Render dashboard → **New → Blueprint**, point it at the repository. It reads
   `render.yaml` and proposes one web service, one 10 GB disk.
3. Fill in the provider keys it asks for — `GROQ_API_KEY`, `GEMINI_API_KEY`,
   `PEXELS_API_KEY`, `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN`. They are
   marked `sync: false`, which is why they are prompted for rather than read out
   of the repo.
4. `LONGHAND_PASSWORD` is generated for you. Read it from **Environment** after
   the first deploy; log in as `longhand`.
5. First build takes a while — it downloads ~480 MB of model weights into an
   image layer. Later builds reuse that layer unless the model stage changes.

The disk mounts at `/data` and `WORKSPACE_DIR` points at `/data/workspace`, so
projects survive deploys. Nothing outside `/data` does.

`FORWARDED_ALLOW_IPS=*` is set for one reason: Render terminates TLS at its edge
and forwards plain HTTP, and Uvicorn ignores `X-Forwarded-Proto` from a peer that
is not in `forwarded_allow_ips` (default `127.0.0.1`). Without it every URL built
from the request — the podcast feed address, and every enclosure inside the feed
— comes out `http://` on an `https://` site. It is safe here because nothing but
Render's proxy can reach the container; it is not safe on a directly reachable
one.

### Starting on the free plan

Free is 512 MB, 0.1 CPU, no disk, and it spins the container down when idle. That
rules out the studio and rules out narration, and it is still enough to serve the
**library**: measured in the container, a reading server sits at **52 MB** with
the `ML=0` image and answers pages without touching the models.

Four edits to `render.yaml`:

| edit | why |
|---|---|
| `plan: free` | — |
| delete the `disk:` block | free plans cannot mount one |
| `AUDIENCE: reader` | mounts no studio routes at all, which is what you want facing the public anyway |
| `ML: "0"` | drops the `ml` extra and the weights: **980 MB against 2.56 GB** |

What you lose, and it is not subtle: **no narration, so no podcast feed.** The
feed lists only chapters that have audio, and audio needs Kokoro — 311 MB
resident, which does not fit beside everything else in 512 MB. Text and briefs
work; briefs call a hosted model, so they cost RAM only for the request.

The second cost is the missing disk. Without one the container filesystem is
wiped on every deploy **and every spin-down**, so a cold start finds an empty
library and the work has to be imported again (~30 s, and it needs egress). Free
is therefore a demo of the reading view, not somewhere to keep anything.

Moving up later is `plan: standard`, the `disk:` block back, and `ML: "1"` — no
code changes, and nothing to migrate because there was nothing to keep.

### After it is up

`GET /healthz` is the only unauthenticated path — a platform health check cannot
carry credentials. It returns a status and a version and reads nothing from the
workspace.

## Self-hosting with Docker

```bash
export LONGHAND_PASSWORD=$(python -c 'import secrets; print(secrets.token_urlsafe(24))')
docker compose up --build
```

Published on `127.0.0.1:8000` only, on purpose. Put a reverse proxy with TLS in
front, or a Cloudflare Tunnel, before letting anything else reach it.

The compose file mounts `assets/music` and `assets/sfx` read-only from the repo.
The tool ships **no audio files** and never will (`docs/audio-design.md`), so
those directories are yours to fill; drop the two lines if they are empty.

## Cloudflare Tunnel — the free path

The container runs on your own machine and Cloudflare publishes it. No hosting
bill, no open ports, no public IP, TLS included, and the renders happen on
hardware you have already paid for — which is the right side of the unit
economics this product has (`docs/multi-tenant-design.md`: *the AI is nearly
free, the compute is the cost*).

The trade is availability, and it is a real one: **the site is up only while
your machine is on and `cloudflared` is running.** That is fine for a tool you
use yourself and wrong for anything anyone else depends on.

### Try it in one command

No account, no DNS, nothing to clean up afterwards. With the container already
running on `127.0.0.1:8000`:

```bash
cloudflared tunnel --url http://localhost:8000
```

It prints a random `*.trycloudflare.com` URL. The URL dies with the process, so
this is for showing someone something, not for running anything.

**The password gate still applies, and it is now the only thing between your
workspace and the internet.** A quick tunnel is a public address. Do not start
one against a container you launched without `LONGHAND_PASSWORD` — and you
cannot, because the container refuses to start that way.

### A tunnel that stays

For a stable address on a domain you own:

```bash
cloudflared tunnel login          # opens a browser, picks the zone
cloudflared tunnel create longhand
```

Then in **Zero Trust → Networks → Tunnels**, add a public hostname
(`longhand.yourdomain.com`) pointing at the service `http://longhand:8000` if
you run `cloudflared` in compose, or `http://localhost:8000` if you run it on
the host. Cloudflare now steers most setups to remotely-managed tunnels, where
that routing lives in the dashboard and the local daemon only carries a token.

The compose file has a `cloudflared` service behind a profile, so it is opt-in:

```bash
export LONGHAND_PASSWORD=$(python -c 'import secrets; print(secrets.token_urlsafe(24))')
export TUNNEL_TOKEN=...            # from the dashboard
docker compose --profile tunnel up --build
```

It joins the same network and reaches the app as `http://longhand:8000`, so
port 8000 never has to be published on the host at all.

### Add Cloudflare Access if other people will use it

Free for up to 50 users. It puts an identity check at Cloudflare's edge, so
requests are authenticated *before* they reach your machine — which is a
meaningfully better position than a shared password, because a brute-force
attempt never gets a TCP connection to your box.

Access and the password gate stack: you would sign in to Access, then the
browser would prompt for the Basic credentials. Two prompts is mildly annoying
and it is defence in depth — the app is still single-user underneath, so leaving
the password on means a misconfigured Access policy cannot expose the workspace
on its own. Turning it off is a decision to trust one layer; do that knowingly,
not by forgetting.

## What the image contains, and why it is large

- **FFmpeg from Debian apt**, because that build has libass, which the
  `subtitles` filter needs to burn captions in. The build asserts the filter is
  present rather than trusting it — M0's very first finding was a stack without
  it, where every render died at the last step after twenty minutes of work.
- **`fonts-dejavu-core`**, the caption face `media/ass.py` names as its default.
  libass silently substitutes something else if it is missing, which is a
  caption-layout bug you only find by looking at a frame.
- **~480 MB of model weights, baked in.** Downloading them at boot would put a
  five-minute stall in front of every cold start and would fail outright on a
  host with no egress to GitHub.
- **A non-root user (`longhand`, uid 10001).** The app writes only under `/data`
  and runs FFmpeg over downloaded stock footage; neither needs more authority.

## What is still single-user

This is a password on the front door, not accounts. There is one workspace, one
worker thread, and one project lock. Two people using the same deployment will
see each other's projects and can queue each other's renders. Real multi-tenancy
is M7+ and is deliberately not started — see `docs/multi-tenant-design.md` for
why, including the unit economics that make it a different product.

`web/media.py` also still has the TOCTOU window recorded in
`docs/superpowers/spike-results-m3.md`. It only matters once the server is
multi-user, which is the same moment auth stops being a single shared password.
