# M2 Spike Results

Definition-of-done verification for M2 (review web UI, three gates, wide only).
Run 2026-08-30/31 on the M1 machine: HP ProBook 450 G10 — i7-1355U (12 threads),
16 GB RAM, Zorin OS 18.1 (Ubuntu 24.04 base), x86_64, Python 3.12.12 via uv,
ffmpeg 6.1.1-3ubuntu5 with libass.

**Everything below is measured with real providers.** Groq wrote the script,
Kokoro spoke it, faster-whisper aligned it, Pexels supplied every shot. No mock
was used anywhere in this run, and no `--providers` override was passed.

**Everything below was driven through a real headless browser** (patchright,
Chromium) against `videomaker serve --port 8822`, not through `TestClient`. That
distinction is the point of this verification: `TestClient` executes no
JavaScript, so it cannot see an htmx swap that never fires or a `<video>` whose
source 404s.

The server ran from a scratch directory outside the repo so the repo's own
`workspace/` was never touched. `.env` had to be copied there too:
`Settings.model_config` is `SettingsConfigDict(env_file=".env")` and
`DEFAULT_CONFIG_FILE` is `Path("config.yaml")` — **both are relative to the
process's working directory, not to the repo root or to the installed package.**
`workspace_dir` defaults to a relative `workspace` for the same reason, which is
what made the scratch run self-contained.

## Definition of done

> A full project via the browser; editing one scene at storyboard re-generates
> only that scene.

**Met, both halves.**

A project went from an empty topic box to a playable
`output/final_wide.mp4` without a single CLI command: created at `/`, gate 1
reviewed and edited at `/projects/{id}/script`, gate 2 reviewed with a shot
swapped at `/projects/{id}/storyboard`, the 480p proxy built and watched at
`/projects/{id}/preview`, and the full-size encode watched to completion at
`/projects/{id}/render`.

```
project   why-ssds-get-slower-as-they-fill-up
topic     "Why SSDs get slower as they fill up"
template  tech_explainer · 1.0 min · af_heart
scenes    5   (s01…s05)
output    output/final_wide.mp4 — 1920×1080, h264 + aac, 54.70 s, 24,022,700 B,
          3.51 Mbit/s, integrated −14.9 LUFS, LRA 2.1 LU, true peak −4.4 dBFS
```

Full-page screenshots were captured from the live server and handed to the
reviewer. They are **not committed** — they are 1.9 MB of PNG showing one
throwaway project, and they date the moment the repo's CSS next changes.

| page | file |
|---|---|
| Gate 1 · Script review, after editing s02 | `gate1-script.png` |
| Gate 2 · Storyboard review, five scenes with players and quota headroom | `gate2-storyboard.png` |
| Gate 3 · Preview review, 480p proxy with burned captions | `gate3-preview.png` |
| Final render, `final_wide.mp4` playing | `render-done.png` |

### The second half, literally

The DoD sentence is about re-generation, so it was checked against file mtimes
rather than against the UI's own claim.

After the project had rendered end to end, one scene's visual was swapped at
`/storyboard` (scene 5, alternative 2 — a 2048×1080 clip) and the run advanced
back to the preview gate. Every per-scene artefact was fingerprinted before and
after:

```
REWRITTEN build/concat_wide.txt        (+2.9s after the click)
REWRITTEN build/narration_wide.wav     (+3.1s)
untouched build/preview_wide.mp4
untouched build/s01_wide.mp4
untouched build/s02_wide.mp4
untouched build/s03_wide.mp4
untouched build/s04_wide.mp4
REWRITTEN build/s05_wide.mp4           (+2.8s)   <- the one scene, and only it
REWRITTEN build/timeline_wide.json     (+2.9s)
REWRITTEN build/video_wide.mp4         (+3.0s)
untouched captions/wide.ass
untouched output/final_wide.mp4
```

**Exactly one scene segment re-encoded.** `s01`–`s04` were left byte-for-byte
alone. The four artefacts that did change are whole-timeline artefacts that
cannot be scene-scoped — the concat list, the timeline JSON, the mixed narration
bed and the joined `video_wide.mp4` — and `captions/wide.ass` correctly did
*not* change, because swapping a picture does not move a word. The whole
re-run took **3.5 s** against **17.1 s** for the same two stages cold.

The swap itself is equally narrow. Before and after the storyboard click that
replaced scene 3's shot, the 15 files under `scenes/` differed by exactly one
entry:

```
> 1788133553.93  scenes/s03/asset.1.mp4
```

One new download, in one scene's folder, 2.1 s after the click. The other
fourteen files' mtimes were unchanged to the nanosecond.

The UI also got the consequences right without being asked: swapping the shot
printed *"That change invalidated work a later gate had already approved, so
those approvals were cleared"*, left gate 2 approved (it was the storyboard that
was edited) and dropped gate 3 back to NOT APPROVED.

## Wall times

Real providers, cold caches, one 5-scene 55-second video.

| step | measured |
|---|---|
| create → script on disk (Groq, cache miss) | **4.4 s** |
| gate 1 page load | 156 ms |
| edit one scene's narration (htmx POST → 200) | < 1 s |
| approve gate 1 → blocked at gate 2 | **55.0 s** |
| ⤷ voice — Kokoro, 5 takes | ~24 s (4.8 s/scene) |
| ⤷ align — faster-whisper, 5 takes | ~14 s (2.8 s/scene) |
| ⤷ visuals — Pexels search + download, 5 scenes | ~6 s |
| gate 2 page load (5 `<video>` players + quota panel) | 471 ms |
| swap one shot at gate 2 (click → file on disk) | **2.1 s** |
| approve gate 2 → blocked at gate 3 | **17.1 s** |
| ⤷ captions | 1.1 s |
| ⤷ assemble — 5 segments + concat + narration mix | 14.7 s |
| build the 480p preview (`-preset ultrafast`) | **9.8 s** → 6.9 MB |
| approve gate 3 → `final_wide.mp4` complete | **47.3 s** → 24.0 MB |
| re-run after the one-scene swap (captions + assemble) | **3.5 s** |
| `videomaker run <id> --yes` afterwards (6 stages cached) | **14.2 s** |

Pipeline work totals **136 s** for the whole video. Elapsed clock from the create
button to the finished render was **8.0 min**; the other ~6 minutes is review
time, which is the product working as intended — the gates exist to be sat at.

The render's own share is the honest headline: **47 s of encode for 55 s of
video**, still `libx264` (see follow-up 5).

## Console errors and failed requests

**Zero console errors or warnings on every page**, every time — index, dashboard,
`/script`, `/storyboard`, `/preview`, `/render`, and each htmx partial fetched
into them.

**Zero server errors.** Across the 269 requests the server logged:

```
126  200
134  206 Partial Content
  6  303 See Other
  2  304 Not Modified
  1  404 Not Found   -> GET /favicon.ico (browser-issued; the app has no icon)
```

No 5xx anywhere, and the only 4xx is the browser asking for a favicon that was
never claimed to exist.

The browser did report failed requests, and they are all one benign shape:

```
net::ERR_ABORTED  /media/{id}/scenes/sNN/asset.mp4
net::ERR_ABORTED  /media/{id}/build/preview_wide.mp4
net::ERR_ABORTED  /media/{id}/output/final_wide.mp4
```

These are the *browser's own* cancellations of `<video>` range requests when the
page is torn down mid-buffer — the storyboard page starts five of them at once
and finishes none. Every corresponding server-side response was `206 Partial
Content`, and fetching the same URL directly returns `200` with the full
8,390,336-byte body. Not a bug; noted here so the next person who sees the
report does not go hunting.

## The full battery

```
$ uv run pytest -q
565 passed, 1 warning in 160.63s (0:02:40)

$ uv run ruff check .
All checks passed!

$ uv run videomaker doctor
python                  OK  3.12.12
ffmpeg                  OK  ffmpeg version 6.1.1-3ubuntu5
ffmpeg subtitles filter OK  libass available
hardware encoder        OK  h264_qsv detected; renders use libx264 (CPU) until
                            fast-render mode lands in M3
disk space              OK  97 GB free
kokoro model files      OK  ~/.cache/ai-video-maker/models/…
LLM API key             OK  groq and/or gemini configured
Pexels API key          OK  configured
                            (8/8 OK — same as M1)

$ uv run videomaker run why-ssds-get-slower-as-they-fill-up --yes
  cached script (1 unit(s) skipped)
  cached voice (5 unit(s) skipped)
  cached align (5 unit(s) skipped)
  cached visuals (5 unit(s) skipped)
  cached captions (1 unit(s) skipped)
  cached assemble (6 unit(s) skipped)
  ran render (0 unit(s) skipped)
status: rendered
```

The last one is the interesting one. **The CLI is unchanged and the two halves
share one project.** `run --yes` picked up a project that had been created,
edited, reviewed and rendered entirely in the browser, agreed with the web UI
about every stage's cache state, re-ran only `render` (the one stage the
storyboard swap had invalidated) and finished in 14.2 s. No source file was
modified during this verification.

The single test warning is upstream and not ours: Starlette deprecating `httpx`
in favour of `httpx2` inside `fastapi.testclient`.

## Quota consumed

`~/.cache/ai-video-maker/quota.json`, differenced across the whole run:

| provider | before | after | consumed |
|---|---|---|---|
| pexels | 10 | 15 | **5 requests** (the five visuals-stage searches) |
| groq | 1 | 1 | **0 recorded** — but one real call was made (see follow-up 1) |
| gemini | — | — | 0 (never reached; groq answered) |
| cloudflare | — | — | 0 (no scene fell through to the image provider) |

Against the soft budgets that leaves `pexels 185/190 this hour`, `groq 28/28 this
minute`, `gemini 240/240 today`, `cloudflare 9000/9000 neurons today` — which is
exactly what the storyboard page's own headroom panel displayed. A whole video
for five stock searches and one LLM call is comfortably inside every free tier.

Note that the two *storyboard swaps* spent nothing: the alternatives were already
persisted per scene from the visuals stage, so re-picking one is a file download,
not an API request. That is the affordance M1's follow-up 9 asked for, and it is
free.

# Follow-ups

## New in M2

### 1. NEW BUG — Groq and Gemini quota is recorded but never persisted

**This is the one real defect this verification turned up. It is pre-existing M1
code, not an M2 regression, and it was deliberately not fixed here.**

`videomaker/providers/llm/__init__.py:178` calls `self.quota.record(...)` after
every LLM request, but **never calls `self.quota.save()`**. Compare
`providers/stock/pexels.py:144-145` and `providers/image/cloudflare.py:170-171`,
which do both. `QuotaTracker.record` only mutates the in-memory dict; `save()` is
what writes `quota.json`.

Because `build_deps` constructs a fresh `QuotaTracker` per job, the in-memory
count does not even survive to the next job in the same process — and here it was
actively destroyed: the script job recorded a groq unit and dropped it, then the
next job's tracker re-read the unchanged file from disk and saved *that* back
when Pexels persisted its own five units.

Evidence that the call was real rather than a cache hit: a new `ResponseCache`
entry was written at `responses/40/40a9f0c313583528.json`, `stored_at
1788133216.24`, holding the generated scene JSON. A hit returns early at line
161 and never reaches `check`/`record` at all.

Consequences:

- `groq`'s `rpm=28` soft budget is enforced only within one process's memory,
  and in practice not even that.
- `gemini`'s `per_day=240` is effectively **never enforced across invocations** —
  every `videomaker run` and every `serve` job starts that counter at zero. This
  is the axis that matters, because a daily cap is precisely the one a sliding
  in-memory counter cannot police.
- The storyboard page's headroom panel and `doctor` under-report LLM usage.

The fix is a one-line `self.quota.save()` beside the `record()` in the `finally`
block, which would make the LLM path match the two providers that already get
this right. It belongs in M3 with the rest of the quota work, and it wants a test
that asserts persistence across two `QuotaTracker` instances rather than one.

### 2. Storyboard alternatives render as labelled placeholders, not thumbnails

Confirmed visually in `shots/gate2-storyboard.png`: the chosen shot in each scene
shows a real frame (it is on disk), while alternatives 2–4 show a grey card
reading `CLIP · 1920×1080 · 25s · USE THIS`. The information is all there and the
swap works, but a human picking between four clips is picking between four
captions.

The cause is a **models** gap, not a web one: `AssetRef`
(`src/videomaker/models.py:40-49`) carries `provider`, `source_id`, `source_url`,
`local_path`, `width`, `height`, `duration_s`, `attribution`, `license` — and no
`preview_url`. `StockResult` (line 56) *does* have one, so Pexels hands us a
thumbnail URL and it is discarded when the candidate is persisted. Real
thumbnails for every candidate therefore means adding `preview_url: str = ""` to
`AssetRef`, carrying it through `providers/stock/`, and having the storyboard
template prefer it — a change to the data model that M2's "no pipeline logic"
constraint put out of scope. (M2's own guard: `/media` serves the project tree
only, so the thumbnail would either have to be fetched by the browser from
`images.pexels.com` or downloaded like the shot itself. That choice belongs with
the models change.)

### 3. `web/media.py` has a theoretical TOCTOU window

`_resolve_within` (line 62) resolves both sides and rejects any escape, then line
84 checks `candidate.is_file()`, then line 122 hands the path to
`FileResponse(target)` which opens it. Between the check and the open, the path
could in principle be replaced by a symlink pointing outside the project tree.

Still not worth closing, and the reasoning has not changed: the server binds
`127.0.0.1`, has no auth by design, and an attacker able to win that race already
has write access to the project directory — which is strictly more than the race
would buy them. It becomes worth an `os.open(..., O_NOFOLLOW)` and a
`FileResponse` over the open descriptor the moment anything makes the server
multi-user or network-reachable, and that is the same moment auth stops being
optional.

### 4. `encode_progress` substitutes a module symbol, and that is only safe while there is one worker

`web/routes/render.py:220-235` rebinds `videomaker.pipeline.render.run_ffmpeg` to
an instrumented wrapper for the duration of one render, then restores it. It is
correct today for one reason and one reason only: **there is exactly one worker
thread running exactly one job at a time**, so the window is genuinely exclusive.
The module docstring says so.

If M3 adds a second worker — and doubling the aspects doubles the encode work, so
it will be tempting — two concurrent renders would fight over one module global
and each would drive the other's progress bar. At that point the honest fix is to
give `run_render` a real `on_progress` parameter and pass it, which means
touching `pipeline/render.py` — legitimate in M3, forbidden in M2. **Do that
before adding the second worker, not after.**

The progress bar itself deserves a note: `ENCODE_FLOOR` is
`stage_progress(STAGE_ORDER[index("render") - 1])` = 6/7, so the render page's
bar starts at ~86% and the whole final encode plays out in the last seventh. It
reads oddly on a page whose only job is that encode (88% displayed while the
message reads `encoding 8.9s of 54.7s`). It is deliberate — the bar tracks the
whole pipeline, not the page — but M3 might reasonably give `/render` a bar
scaled to the encode it is actually watching.

## Carried forward from M1, still open

### 5. `doctor` detects `h264_qsv` but every render uses `libx264`

Unchanged and still accurate. `doctor` reports *"h264_qsv detected; renders use
libx264 (CPU) until fast-render mode lands in M3"*, and this run's output confirms
it: `ffprobe` shows `codec_name=h264` from a libx264 encode. 47 s of the render
and 15 s of the assemble are CPU encode. Hardware fast-render is M3 work, and M3
is where it pays double because M3 adds the 9:16 aspect.

### 6. Stock relevance is keyword-literal, ~6/10 on topic

Unchanged. This run's five shots were better than M1's ten (a monitor showing a
streaming grid for "SSD copies a file", a spinning HDD platter for the
spare-blocks scene, a NAND-looking macro for the page/block scene), but the
pattern is the same: Pexels matches words, not concepts. What M2 adds is the
*remedy* — the storyboard gate lists four alternatives per scene with a free
re-pick and a live re-search box, and swapping one costs nothing. M3's image
fallback is still the right answer for a scene whose four candidates are all
wrong.

### 7. `QuotaTracker` uses a sliding 24 h window, not a calendar day

Unchanged. `_prune`'s cutoff is `clock() - 86400`, and `per_day` budgets are
summed over the trailing 24 h. Gemini and Cloudflare both reset on a calendar day
(UTC and, for Cloudflare, its own account timezone), so the tracker is
conservative in the morning and can refuse a call the provider would have
allowed. Deliberately conservative — over-counting costs headroom, never money —
but M3 should reset `per_day` on a UTC calendar boundary now that a UI displays
the number to a human who can compare it against the provider's dashboard.
Follow-up 1 is the prerequisite: there is no point making the daily window
accurate while the daily counter is not being written down.

### 8. The repo-root `templates/` directory is not packaged into a wheel

Unchanged and still true: `pyproject.toml` has
`[tool.hatch.build.targets.wheel] packages = ["src/videomaker"]`, which ships the
package — including `web/templates/` and `web/static/` — but not the repo-root
`templates/` holding `tech_explainer.yaml` and `_schema.md`. `videomaker serve`
works from a checkout and would find no templates from a `pip install`. M6
(packaging and Docker) owns this; nothing in M2 makes it more urgent, but the web
UI does make it more visible, because the create form's template dropdown would
simply be empty.

## What did not need changing

- **No source code was modified.** `git status` showed only this document and the
  plan's own checkboxes throughout.
- The repo's `workspace/` was never touched; the entire run lived in a scratch
  directory with its own copy of `.env`.
- `doctor` reported the same 8/8 OK before and after.
- 565 tests and `ruff check` were green before and after.
