# Multi-tenant service — what changes

Owner decision: the deployed app should allow **public signup**, with an admin
who manages users, sees statistics, and creates templates.

This is a different product from the one M0–M3 built, not a feature on top of it.
Everything below is what actually has to change, with the measured numbers that
make each item real. **None of it belongs in M3.**

---

## 1. The economics, and the one design that survives them

Every free tier this project depends on is **per-account, not per-user**:

| provider | soft budget | shared across every signup |
|---|---|---|
| Groq | 28 rpm | yes |
| Gemini | 240/day | yes |
| Pexels | 190/hour | yes |
| Cloudflare Workers AI | 9,000 neurons/day | yes |

M1 measured one 2-minute video at 1 Groq call, 10 Pexels requests, 0 neurons —
cheap. But ten users making three videos a day is 300 Pexels requests an hour
against a 190/hour budget, and the Gemini daily cap goes in an afternoon. The
first user of the day would work; the rest would see `QuotaExceeded`.

**Three ways out. The first is the only one that keeps the $0 promise.**

### (a) Bring your own keys — recommended

Each user supplies their own free-tier keys at signup. The operator pays nothing
for inference, ever. Every provider used here has a free tier with no credit
card, so the barrier is a signup form and five minutes, not money.

This fits the project better than the alternatives on every axis: it preserves
the $0 constraint that shapes the whole codebase, it removes any incentive to
abuse the service for free compute, it needs no billing, and it matches the
AGPL/self-host ethos already in the README.

Implementation is mostly already there: `Settings` reads keys from the
environment today, so it becomes per-user settings loaded per request instead of
process-wide. **Keys must be encrypted at rest** and never rendered back to the
browser — an app holding other people's API keys is a credential store, and has
to behave like one.

### (b) Operator pays, funded by subscriptions

**The owner has said they want subscriptions for end users eventually**, which
points here. Paying customers reasonably expect it to just work, so asking them
for their own API keys stops being attractive the moment money changes hands.

That makes the unit economics the thing to get right first — see section 1a.

### (c) Freemium

A small shared free allowance from your pool, paid beyond. Sensible **only after**
(b) exists and its costs are known; before that it is the worst of both, since you
carry billing complexity *and* still get the free tier drained.

A better-shaped free tier for this product: **bring-your-own-keys is the free
plan**, and the subscription is "we run it for you." The free user costs you
nothing but storage, and the upgrade is a real convenience rather than a
withheld feature.

---

## 1a. Unit economics — the surprising part

**The AI is nearly free. The compute is the cost.** Measured on the real
pipeline (M1/M3 spike results), per 2-minute video:

| component | measured | cost at paid rates |
|---|---|---|
| LLM script | 1 Groq call | fractions of a cent |
| TTS (Kokoro) | 72 s CPU | *no API cost — CPU only* |
| STT (whisper) | 22 s CPU | *no API cost — CPU only* |
| Stock (Pexels) | 10 requests | free tier, or a paid plan |
| AI images (Flux) | **0 neurons** — stock covered every scene | ~$0.006 if 10 images were needed |
| **Encode** | **~70 s of libx264 on 12 threads** | **this is the bill** |
| Storage/egress | 61 MB wide + 66 MB vertical | egress is the sleeper cost |

So this is a **compute business, not an AI-API business** — the opposite of what
most people assume when pricing an "AI video" product. Three consequences:

1. **Price against CPU-minutes, not against API calls.** A 2 vCPU VPS renders a
   2-minute video in roughly 10 minutes wall clock (M1's 184 s was on 12 threads).
   One such box, realistically utilised, is on the order of 100 videos a month.
2. **Hardware encode moves the margin, not the features.** M3 Task 18 adds
   QSV/VA-API fast mode; most cheap VPS instances do not expose it, so an
   encode-capable machine is an infrastructure decision with a direct effect on
   gross margin. Worth benchmarking before choosing a host.
3. **Egress will surprise you.** ~127 MB per project across both aspects, every
   download and every preview scrub. A 1 TB allowance is ~8,000 downloads; past
   that it is metered. Serve outputs from object storage with sane caching rather
   than from the app server.

The honest version of a price: a subscription has to cover render minutes and
egress with margin, and the AI line is a rounding error. Get one real box, render
fifty videos, and measure — the numbers above are from a 12-thread laptop and are
the optimistic end.

---

## 2. What breaks technically

### Rendering does not scale the way the current design assumes

M1 measured a 2-minute video at **184 s cold on 12 threads**, with **70 s of that
in libx264**. A typical VPS has 2 vCPU. The same video is plausibly 10+ minutes
there.

The current scheduler is **one daemon worker thread, one job at a time**
(M2 design decision 1) — deliberately, because rendering saturates the CPU and a
second concurrent job makes both slower. That is correct for one person on a
laptop and is a hard ceiling for a public service: ten users queued behind one
worker is hours of waiting.

This needs a real job system — a queue that survives restarts, workers that can
scale horizontally, and per-user fairness so one long series does not starve
everyone else. That is the single largest piece of work in this document, and it
invalidates a design decision the current code is built around.

Hardware encode (M3 Task 18, QSV/VA-API) helps materially and most VPS instances
do not have it. Budget for CPU encoding.

### Storage

M1 measured **1–3 GB of intermediates per project**, and `doctor` warns below
20 GB free. A hundred users with a few projects each is terabytes. Needs object
storage for outputs, aggressive retention on `build/` (M3 Task 16's `clean` is the
seed), and a quota per user.

### Isolation becomes load-bearing

Today `ProjectStore` is directories on local disk and any request can reach any
project. Multi-tenant means **ownership on every route**, not just the media one —
each of the 24 routes needs an authorisation check, and the failure mode of
missing one is reading or deleting a stranger's work.

Two things already flagged become urgent rather than theoretical:

- **`web/media.py`'s TOCTOU window** (M2 Task 2). It was correctly deferred with
  the note *"becomes worth closing the moment anything makes it multi-user, which
  is the same moment auth stops being optional."* That moment is this document.
- **`project.json` on a shared filesystem.** `fcntl.flock` coordinates processes
  on one machine; it does not coordinate two app servers. Multiple workers means
  a real database or a lock service, and the spec's *"deliberately absent in v1:
  databases"* stops holding.

### The admin surface the owner asked for

- **Statistics** — genuinely cheap and worth building even for a single user. The
  data is already on disk: project count, scene counts, durations, render wall
  times, and `quota.json` with real per-provider spend. No new instrumentation.
- **Template creation** — templates are schema-validated YAML with `_schema.md`
  already written. A form that writes and lints one is small; M4 already owns
  templates and `templates lint`. **Note templates are currently a repo directory
  and are not packaged into a wheel** (the M6 gap) — user-created templates need a
  writable location outside the package, which is a change worth making anyway.
- **User management** — list, suspend, delete, see per-user usage. Straightforward
  once a user model exists; meaningless before.

---

## 3. Abuse, stated plainly

A public tool that generates narrated video at scale will be used to generate
narrated video at scale. The project's entire positioning is the opposite of that
— three human gates, original content, explicitly not a spam farm — and the
README says so.

The three gates help: they make bulk generation slow and manual by design. Bring
your own keys helps more, because it removes the free-compute incentive that
drives most abuse of hosted generators. Beyond that, a public deployment needs a
terms of service, a way to receive reports, and the ability to suspend an account
— which is the same user-management surface listed above, so it is not extra
work, only extra intent.

Worth deciding early rather than after the first incident.

---

## 4. Sequencing — the recommendation

**Do not start this before the single-user product is finished and used.**

1. **Finish M3–M5** as planned. Vertical, audio, visual quality, metadata.
2. **Publish the computer-literacy series** (`docs/series-computer-literacy.md`).
   Five to eight episodes will teach you more about what this product should be
   than any amount of multi-tenant architecture.
3. **M6 Docker** — a prerequisite regardless, and it is where the single-password
   gate belongs so *you* can deploy safely.
4. **Then M7+ multi-tenant**, if the answer to step 2 was yes.

The single-user tool is nearly finished and is the thing that proves the concept.
A public service built on an unvalidated product is the expensive order to do this
in — and every piece of work above is cheaper once the product is settled, because
none of it has to be redone when the pipeline changes.

**One thing to bring forward regardless:** the bring-your-own-keys refactor —
`Settings` resolved per request rather than per process. It is small now, it is
invasive later, and it is useful even single-user (a second project with a second
Cloudflare account, say).
