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

**The blueprint deploys a free reading server.** That is a deliberate default:
free is where you find out whether anyone wants this, and it is the shape that is
safe to put on a public URL. See *Running the whole thing* below for the studio.

1. Push this repo to GitHub (it already is).
2. Render dashboard → **New → Blueprint**, point it at the repository. It reads
   `render.yaml` and proposes one web service on the free plan, no disk.
3. Fill in the provider keys it asks for — `GROQ_API_KEY`, `GEMINI_API_KEY`,
   `PEXELS_API_KEY`, `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN`. They are
   marked `sync: false`, which is why they are prompted for rather than read out
   of the repo. Only the LLM keys do anything on a reading server: they write the
   briefs.
4. `LONGHAND_PASSWORD` is generated for you. Read it from **Environment** after
   the first deploy; log in as `longhand`.
5. It imports the World English Bible on first boot and publishes it, so the
   shelf is not empty when you open it. That is `IMPORT_WORKS` in the blueprint —
   see below.

`FORWARDED_ALLOW_IPS=*` is set for one reason: Render terminates TLS at its edge
and forwards plain HTTP, and Uvicorn ignores `X-Forwarded-Proto` from a peer that
is not in `forwarded_allow_ips` (default `127.0.0.1`). Without it every URL built
from the request — the podcast feed address, and every enclosure inside the feed
— comes out `http://` on an `https://` site. It is safe here because nothing but
Render's proxy can reach the container; it is not safe on a directly reachable
one.

### What free actually gives you

Measured in the container, not estimated: a reading server on the `ML=0` image
sits at **52 MB** resident and answers `/healthz`, the shelf, a chapter, the feed
and the zip without touching a model.

What it cannot do, and none of it is subtle:

**No narration, so no podcast feed.** Kokoro is 311 MB resident and the box is
512 MB. The feed lists only chapters that have audio, so on free it is a valid,
empty channel. The zip still works — it bundles the text.

**Nothing is kept.** Free has no disk, so the filesystem is wiped on every deploy
*and every spin-down*. A cold start finds an empty library and the work has to be
imported again (~30 s, and it needs egress).

**It sleeps.** Render spins a free instance down when idle, so the first request
after a quiet spell waits for a cold start — and then re-imports, because the
previous filesystem is gone.

**Set `READER_COOKIE_SECRET`.** Empty falls back to a per-process key, so every
reader's bookmark and remembered mode is invalidated the moment the container
restarts — which on a plan that sleeps is most visits. The blueprint generates
one; a service created by hand needs it set. It signs a cookie and nothing else,
so rotating it costs people their places and nothing more.

**`IMPORT_WORKS` is how anything gets on the shelf.** A reading server mounts no
route that can import, and a free container has no shell to run the CLI in, so
this is the only way in: a comma-separated list of catalogue ids, imported in the
background at startup and published. It is set to `web` in the blueprint. `bsb`
is the other blessed text; naming both costs another download on every cold
start. The import runs on the job queue, so `/healthz` answers while it works —
a boot blocked on a Bible download is a container the platform kills before it
ever replies.

Briefs do work: they call a hosted model, so they cost RAM only for the length of
the request.

### Running the whole thing

Voices, renders, and projects that survive a deploy. Four edits to `render.yaml`,
no code changes, and nothing to migrate because free kept nothing:

| edit | why |
|---|---|
| `plan: standard` | 2 GB / 1 CPU. Kokoro (311 MB) and faster-whisper (142 MB) are both resident while a project is voiced and aligned |
| `ML: "1"` | builds the image with the model stack: 2.56 GB against 980 MB |
| drop `AUDIENCE: reader` | mounts the studio: create, gates, runner, render |
| put the `disk:` block back | `workspace/` is the entire product state; without it every project vanishes on the next deploy |

```yaml
    disk:
      name: longhand-workspace
      mountPath: /data
      sizeGB: 10
```

10 GB is about five finished projects at the ~390 MB M3 measured;
`videomaker clean` reclaims the build intermediates when it gets tight.

**Do not put the studio on a public URL casually.** It can spend your provider
quota and it runs FFmpeg on downloaded footage. `LONGHAND_PASSWORD` is the whole
gate, and the container refuses to start without it.

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
