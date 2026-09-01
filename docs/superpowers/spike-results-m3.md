# M3 Spike Results

Definition-of-done verification for M3 (dual format, audio, visual search,
polish). Run 2026-09-01 on the M1/M2 machine: HP ProBook 450 G10 — i7-1355U
(12 threads), 16 GB RAM, Zorin OS 18.1 (Ubuntu 24.04 base), x86_64, Python
3.12.12 via uv, ffmpeg 6.1.1-3ubuntu5 with libass, `h264_vaapi` available.

**Everything below is measured with real providers.** Groq wrote the script,
Kokoro spoke it, faster-whisper aligned it, Pexels supplied every shot. No
`--providers` override was passed anywhere in this run.

Two projects were driven end to end, both in a scratch tree outside the repo so
that **the owner's ten real projects in `workspace/` were read and never
written**. `videomaker clean` was never pointed at `workspace/`.

| project | what it is | why |
|---|---|---|
| **`what-the-cloud-…-your-documents`** | a *copy* of one of the owner's ten, re-run from a deleted stage cache | 10 scenes, 5 of them `in_short` — the Short is a real selection, not the whole video |
| **`why-file-names-matter-more-than-folders`** | brand new, nothing cached anywhere | the genuinely cold number: 1 Groq call, 10 Pexels searches, real downloads |

The scratch tree needed its own `config.yaml` (for `music_dir`/`sfx_dir`) and a
symlink to `.env`, for the reason M2 recorded: both paths are resolved against
the **process's working directory**, not the repo root or the installed package.

**The audio library was seeded with ffmpeg-generated files.** The project ships
no audio and never will (`docs/audio-design.md`); a 45 s sine pad, a 32 s second
track, a brown-noise whoosh and a sine ping were synthesised into the scratch
`assets/`, with `library.yaml` entries, so the music and SFX paths could be
exercised at all. Nothing audio was added to the repository.

---

## Definition of done, clause by clause

> One project produces both `final_wide.mp4` and a ≤3-minute `final_vertical.mp4`
> plus a thumbnail; captions are styled per aspect and never scaled from the
> other; music ducks audibly under narration; and the stock footage the pipeline
> picks on its own is measurably better than M1's 6/10.

| clause | verdict |
|---|---|
| both cuts plus a thumbnail, from one project | **met** |
| the Short is ≤ 3 minutes | **met** — and the limit refuses rather than truncating, verified live |
| captions styled per aspect, never scaled | **met as specified — but the vertical style as shipped overflows the frame.** See defect 2 |
| music ducks audibly under narration | **met, measured: 12.81 dB on the wide cut, 12.16 dB on the Short, against a 12 dB target** |
| stock footage measurably better than M1's 6/10 | **NOT MET. 3.4/10 against an 8/10 target, statistically indistinguishable from the M1 path it replaced** |

Four of five. The fifth is the one the milestone was named for, and it is a
miss, not a shortfall — see [Visual relevance](#visual-relevance-the-clause-that-failed).

### Both cuts and a thumbnail

`what-the-cloud-actually-means-for-your-documents`, one `videomaker run … --yes`:

```
output/final_wide.mp4      1920×1080  h264 High yuv420p 30 fps  aac-lc 48 kHz stereo 192 k
                           116.17 s   51,933,881 B   3.58 Mbit/s   −14.0 LUFS, LRA 2.3 LU
output/final_vertical.mp4  1080×1920  h264 High yuv420p 30 fps  aac-lc 48 kHz stereo 192 k
                            55.30 s   30,516,683 B   4.41 Mbit/s   −14.0 LUFS, LRA 2.3 LU
output/thumbnail.jpg       1280×720   mjpeg  122,058 B
```

`why-file-names-matter-more-than-folders`, cold in every cache:

```
output/final_wide.mp4      1920×1080  113.37 s  54,291,312 B  −14.0 LUFS
output/final_vertical.mp4  1080×1920   55.17 s  24,441,269 B  −14.0 LUFS
output/thumbnail.jpg       1280×720            86,353 B
```

The Short is a **selection**, not a crop of the long video: 5 of 10 scenes,
55.30 s against the wide cut's 116.17 s. `build/` holds ten `sNN_wide.mp4` and
five `sNN_vertical.mp4`, each re-cropped from its own source asset.

### The three-minute limit refuses, it does not truncate

Exercised live rather than asserted. One scene's `duration_s` was inflated in
the scratch copy until the `in_short` subset summed past the limit:

```
error stage 'render' failed: the Short runs 185.3s, over the 180s limit for
output/final_vertical.mp4: untick "in short" on s01 (140.5s), or on any scenes
totalling 5.3s, and render again. Nothing is truncated — a Short cut off at the
limit ends mid-sentence.
```

**And `final_wide.mp4` was still on disk afterwards, 52.8 MB, freshly written.**
That is `run_render`'s `finally` doing exactly what its docstring claims: an
over-long Short does not cost you the wide render you had just paid for, and the
next run does not re-encode it to reach the same refusal.

Neither real project came near the limit (55.30 s and 55.17 s), so the guard was
never load-bearing in ordinary use. Gate 2 surfaces the distinction the design
draws — `THE SHORT · 0:55 OF 3:00` beside *"Past the 0:45 mark where Shorts hold
attention. Nothing is blocked — only the limit refuses — but a tighter cut
travels further."*

### Captions are styled per aspect

Measured from the rendered pixels, not read off the config. White-pixel row
extents in mid-scene frames of both finished cuts:

| | wide | vertical |
|---|---|---|
| declared `font_size` | 64 | 96 |
| measured glyph-band height | 38–52 px | 63–80 px |
| declared `words_per_chunk` | 5 | 3 |
| **measured** words per line | mean 4.55 / 4.89, max 5 | mean 2.91 / 2.88, max 3 |
| caption lines in the same project | 58 | 45 |
| measured band centre | **82.0–82.5 %** of frame height | **62.3–62.8 %** of frame height |
| design intent | lower third | 62.0 % |

The two aspects are chunked from the word timings **independently** — 58 lines
against 45 for the same narration — and land in structurally different places:
lower third against just above centre. Neither is a scale of the other. The
clause is met.

### Music ducks audibly — measured

The method is `tests/integration/test_render_music.py`'s, applied to a real
render: put the bed in a frequency band the narration does not occupy, then
band-pass the finished mp4 and compare the bed's level while someone is talking
against its level in a real pause. Windows come from `silencedetect` on the
actual narration bed, offset past the compressor's 20 ms attack and 300 ms
release.

**The first attempt failed and is worth recording.** The integration test
high-passes at 2 kHz, which is clean against its synthetic low-passed narration
but not against real speech: a narration-only render measured **−26.4 dBFS**
above 2 kHz, swamping the music. Banding the narration by octave showed it holds
−34 to −38 dBFS everywhere below 12 kHz and only falls away above it. The bed
was regenerated as white noise banded **14–16 kHz**, where the same narration
measures −58.9 dBFS — 34 dB below the music — and the read became clean.

Four renders of the same project, differing only in `music.duck_db`:

| render | music in the gaps | music under speech | **measured duck** |
|---|---|---|---|
| no music at all | −inf (silence) | −58.86 dBFS (speech leakage only) | n/a |
| `duck_db = 0` | −30.56 dBFS | −30.55 dBFS | **−0.01 dB** |
| `duck_db = 12` *(the shipped default)* | −24.74 dBFS | −37.55 dBFS | **12.81 dB** |
| `duck_db = 24` | −23.47 dBFS | −45.63 dBFS | **22.16 dB** |
| `duck_db = 12`, **the Short** | −24.88 dBFS | −37.04 dBFS | **12.16 dB** |

Read the controls first. With ducking off the bed is flat to within **0.01 dB**
whether or not anyone is speaking, so the effect is the compressor and not the
material. With no music the band holds nothing but 34 dB of speech leakage, so
the numbers above are reading the music. And the Short is ducked by its own mix,
not the wide cut's.

The shipped default asks for 12 dB and delivers **12.81 dB**; 24 delivers 22.16.
`media/audio.py`'s claim — a target rather than a guarantee, calibrated against
`NOMINAL_SPEECH_DBFS` — holds within about a decibel in both directions. 12.8 dB
is roughly a halving of perceived loudness; it is not a subtle effect.

**The two-pass loudness fix (M1 follow-up 11) is confirmed in the same
measurement.** Every cut with music lands at exactly **−14.0 LUFS**, wide and
vertical, on both projects. The no-music render of the identical project lands
at **−14.8 LUFS** — the single pass, still missing by 0.8 LU, exactly the
0.9 LU M1 recorded. Two passes run only when there is music, and when they run
they hit the target.

### `crop_focus_x` steers the Short's window, to the pixel

The vertical window's position was measured rather than eyeballed: the finished
vertical segment was matched against the wide segment of the same scene by
sliding a candidate crop across the wide frame and minimising MSE.

| scene | `crop_focus_x` | predicted x | **measured x** | delta | implied focus |
|---|---|---|---|---|---|
| s05 | 0.5 (default) | 656.0 px | **656 px** | 0.0 | 0.500 |
| s01 | 0.5 (default) | 656.0 px | **656 px** | 0.0 | 0.500 |
| s05 | 0.85 | 1115.2 px | **1116 px** | +0.8 | 0.851 |
| s01 | 0.15 | 196.8 px | **196 px** | −0.8 | 0.149 |

A 1920-wide source yields a 608 px window; the parameter places it exactly where
it says, to within the sub-pixel rounding of `crop`. Looking at the frames: at
0.5 the s05 window sits on a featureless dark rack front; at 0.85 it sits on the
patch panel and NAS. The parameter is not decorative — it is the difference
between a usable Short frame and a wasted one.

**One scene's `crop_focus_x` change rebuilds four units**, verified by mtime
diff: that scene's segment in *both* aspects, plus both aspects' concat,
narration and joined video. The other thirteen segments are untouched. See
follow-up 4 — the wide half of that is avoidable.

### The vertical crop keeps the subject

Frames extracted mid-scene from `final_vertical.mp4` and looked at. All five
Short scenes keep their subject inside the 9:16 window: the laptop screen and
the hands over it (s01), the rack (s05), the phone held in frame (s06), the
radio-telescope dish (s07), the cloud (s10). Nothing is cropped to a shoulder or
an empty wall — helped by the fact that Pexels stock is overwhelmingly
centre-composed, which is luck the tool is currently living off rather than
something it checks.

---

## Visual relevance — the clause that failed

**Not measured again here.** `docs/visual-relevance-baseline.md` is the
measurement, taken 2026-08-31 by `tests/quality/test_visual_relevance.py`, and
re-running it costs the day's Gemini budget for a number that will not move.
Restating its result without softening it:

| arm | strongly on topic |
|---|---|
| M1's own code on the ten finished projects (`before`) | **3.6/10** |
| M1 selection with M3 queries (`before_same_query`) | 3.1/10 |
| **Task 12's ladder + cliché drop + ranking — the shipped default** | **3.4/10** |
| Task 12 + Task 13's Gemini re-rank | 3.4/10 |

Against a target of **8/10**.

Three things have to be said plainly.

1. **The shipped default is not distinguishable from what it replaced.** The
   paired comparison moves 21 of 100 scenes, 12 up and 9 down: McNemar exact
   **p = 0.66**. Task 12 changed the chosen clip on 74 of 100 scenes and changed
   the quality of the choice on none.
2. **M1's "6/10" was one project of ten.** The same judge, calibrated to
   reproduce M1's hand rating scene for scene, scores the whole fleet at 3.6/10.
   `how-ssds-work` happened to be the best of the ten. The honest baseline the
   plan should have been written against is 3.6/10, and the gap to 8/10 is more
   than twice what the plan assumed.
3. **The failure mode is a homonym problem and neither end of Task 12 touches
   it.** `NAND flash cell array` returns a solar panel array; `journal log file
   view` returns a woman cutting a tree log; `uniform bullet point slide`
   returns a playground slide. Specificity is what *creates* the homonym, and
   the ranker cannot fix a candidate set that has no right answer in it. The
   second, larger class — 48 of 100 "generic" picks — is a supply problem:
   Pexels has no footage of software UI, so the best available answer for
   `save dialog window` really is a person at a laptop.

This verification's own frames illustrate it without needing the harness. Of the
ten shots in the wide cut, `solid state drive platter` returned a **spinning
magnetic hard-disk platter** (the wrong storage technology, in a video about the
cloud), `WiFi router antenna` returned a **radio telescope in a field of
heather**, and `cloud sculpture installation` returned **literal clouds in the
sky**. Three of ten wrong in exactly the way the baseline document predicts.

**Do not move the goalpost.** 8/10 remains the bar; 3.4/10 is where the tool is,
and gate 2 stays slow until that number moves. The starting point for M4 is
`docs/visual-relevance-baseline.md`, **not** `docs/visual-search-design.md` —
the design document's item 1 ("better instructions") is the lever the
measurement proved is not a lever.

---

## Wall times

Per stage, timed by invoking `run --until <stage>` once per stage, so each row
carries ~0.5 s of CLI import (M1 measured the same overhead the same way).

| stage | **cold, new project** | copy, warm response cache | notes |
|---|---|---|---|
| script | **5.93 s** | 0.73 s | one Groq call cold; a cache hit warm |
| voice | **71.35 s** | 74.67 s | Kokoro, 10 scenes, ~111 s of audio → RTF ≈ 0.64 |
| align | **39.34 s** | 40.92 s | faster-whisper base/int8/CPU → RTF ≈ 0.35, incl. one model build |
| visuals | **11.24 s** | 5.27 s | 10 Pexels searches + 10 downloads cold; searches cached and assets on disk warm |
| captions | **0.46 s** | 0.30 s | two `.ass` files, pure string building |
| assemble | **50.22 s** | 50.60 s | **15** segments (10 wide + 5 vertical) + two narration beds + two concats |
| render | **68.87 s** | 70.57 s | two encodes, two ASS burn-ins, two loudness passes each |
| thumbnail | **1.43 s** | 1.56 s | one frame, one gradient, one Pillow text pass |
| **total** | **248.84 s** | **244.62 s** | |

| | |
|---|---|
| cold total | **248.84 s** for 113.4 s wide + 55.2 s vertical + a thumbnail |
| warm re-run (everything cached) | **0.81 s** (1.38 s first, page cache cold) |
| cold : warm | **≈ 300×** |

M1's cold total was 184.14 s for a wide-only 115.9 s video. M3 adds a second
aspect for **+35 %**, not +100 %: the Short is 5 scenes rather than 10, and
`assemble` and `render` are the only stages that grew. `voice`, `align` and
`visuals` are per scene and aspect-blind, which is the whole point of vertical
being a second *cut* rather than a second *pipeline*.

`align` is slower than M1's 21.71 s (RTF 0.196 → 0.35). Nothing in M3 touched
align; the machine was under more concurrent load during this run than during
M1's, and the model build is amortised the same way. Not investigated further.

### The fast-render benchmark (Task 18)

`videomaker run --fast`, same project, same content, `h264_vaapi` confirmed in
the output's encoder tag rather than assumed:

| | libx264 (default) | `--fast` / `h264_vaapi` | ratio |
|---|---|---|---|
| assemble — wall | 54.18 s | **27.58 s** | **1.96× faster** |
| assemble — CPU (user+sys) | 325.2 s | **84.7 s** | **3.84× cheaper** |
| render — wall | 68.94 s | **59.99 s** | 1.15× faster |
| render — CPU (user+sys) | 325.7 s | **98.1 s** | **3.32× cheaper** |
| **both stages — wall** | **123.12 s** | **87.57 s** | **1.41× faster** |
| **both stages — CPU** | **650.9 s** | **182.8 s** | **3.56× cheaper** |
| `final_wide.mp4` size | 54,291,312 B | 68,682,715 B | **+26.5 %** |
| `final_vertical.mp4` size | 24,441,269 B | 24,409,480 B | −0.1 % |
| SSIM, vaapi against libx264 | — | **0.9900** wide, **0.9926** vertical | |

Three things this says that `config.example.yaml`'s comment does not.

* **The win is concentrated in `assemble`, not `render`.** `assemble` is almost
  pure encode and nearly doubles. `render` also decodes the whole cut twice for
  `loudnorm`'s measuring pass and rasterises ASS subtitles through libass, and
  neither of those moves to the GPU — so the stage the user waits on longest is
  the one `--fast` helps least. The comment's "halved the segment encode" is
  right; a reader could easily take it to mean the run halves, and it does not.
* **CPU falls ~3.6×**, which matches the comment's "roughly fourfold" and is the
  real reason to want it: on a 12-thread laptop the libx264 path pins six cores
  for two minutes.
* **The size penalty is content-dependent, not a flat 20 %.** The wide cut grew
  26.5 %; the vertical cut, from the same footage at the same qp, did not grow at
  all. A single "a fifth more bytes" figure is optimistic on busy 16:9 material.

Both encoders were verified by container tag (`Lavc60.31.102 h264_vaapi` against
`Lavc60.31.102 libx264`), because `doctor` listing an encoder and an encoder
actually opening are the distinction Task 18 exists to make.

---

## The full battery

```
$ uv run pytest -q
1323 passed, 2 skipped in 524.01s (0:08:44)     # before this task's fix
1324 passed, 2 skipped in 636.64s (0:10:36)     # after: +1 regression test

$ uv run ruff check .
All checks passed!

$ uv run videomaker doctor
python                   OK  3.12.12
ffmpeg                   OK  ffmpeg version 6.1.1-3ubuntu5
ffmpeg subtitles filter  OK  libass available
hardware encoder         OK  h264_vaapi opens; `videomaker run --fast` encodes with it
caption font             OK  DejaVu Sans -> /usr/share/fonts/truetype/dejavu/…
caption script coverage  OK  English, Spanish, French, Italian, Portuguese all draw
disk space               OK  92 GB free
audio library            OK  empty (assets/music, assets/sfx) — renders are narration only
kokoro model files       OK  ~/.cache/ai-video-maker/…
LLM API key              OK  groq and/or gemini configured
Pexels API key           OK  configured
```

The three warnings pytest reports are `RerankUnavailable` raised deliberately by
`tests/unit/test_visual_rerank.py`, proving the re-rank's fallbacks are no longer
silent (Task 13's fix). They are the test asserting, not the suite complaining.

### The web UI, through a real browser

patchright/Chromium against `videomaker serve --port 8791` on the scratch
workspace — not `TestClient`, for the reason M2 gives.

| page | status | body | notes |
|---|---|---|---|
| `/` | 200 | — | |
| `/projects/{id}` | 200 | 1,712 chars | 3 `data-gate`, 3 `data-approved` |
| `/projects/{id}/script` | 200 | 2,248 chars | |
| `/projects/{id}/storyboard` | 200 | 4,923 chars | **20 `<video>`, 30 `<img>`** |
| `/projects/{id}/render` | 200 | 616 chars | 1 `<video>` |

**Zero console errors and zero console warnings on every page.**

The 50 "failed requests" the browser reported are all `net::ERR_ABORTED` on
`<video>` sources — the browser abandoning media range requests as it navigates
on. The endpoint itself is healthy, checked directly: full GET 200 with the
complete 8,709,965 bytes, `Range: bytes=0-1023` → **206** with 1,024 bytes,
`final_wide.mp4`/`final_vertical.mp4`/`thumbnail.jpg` all 200, and a
`../../../etc/passwd` traversal → **404**.

**M2 follow-up 2 is closed**: the storyboard's alternatives render as 30 real
thumbnails rather than grey placeholder cards, off `AssetRef.preview_url`
(Task 2). Gate 2 also carries the Short's duration meter, the per-scene
`in the short` / `left out of the vertical cut` state, the vertical safe-area
guides drawn over each player, and a live free-tier headroom panel that
correctly showed `180 of 190 requests left this hour` after the cold run spent
exactly ten.

---

## Quota consumed

`~/.cache/ai-video-maker/quota.json`, before and after, on a fresh UTC day:

| provider | spent | soft budget (published) | notes |
|---|---|---|---|
| **Groq** | **1 request** | 28 rpm (30) | one script call for the new project; no repair retry |
| **Pexels** | **10 requests** | 190/hour (200) | one search per scene — the ladder stopped at rung 1 on all ten |
| **Gemini** | **0** | 240/day (250) | LLM fallback never reached; the re-rank is off |
| **Cloudflare** | **0 neurons** | 9 000/day (10 000) | Pexels satisfied all ten scenes, so Flux never ran |

Peak usage was 10 of 190 on the Pexels hourly window. **The entire verification —
two projects, both cuts each, two thumbnails, plus more than a dozen further renders for the duck, the SFX,
the three-minute limit and the fast-render benchmark — cost one LLM call and ten
stock searches.**

The copied project cost **literally nothing**: every Groq and Pexels response it
needed was already in the 235-entry, 19 MB response cache. That is worth naming
as a hazard as well as a feature — see follow-up 3.

Disk after the run: 390 MB for the ten-scene project (build 165 MB, scenes
147 MB, output 79 MB); stage cache 91 entries (53 stage fingerprints, 38
`status:`-prefixed); shared model cache 479 MB unchanged.

---

# Defects found by this verification

## 1. FIXED — a non-empty `assets/sfx/` made every render fail

**This is the one hard failure the verification turned up, and it is fixed here
because nothing else in the milestone could be demonstrated around it.**

The first full run died at `render`:

```
Error opening input: No such file or directory
Error opening input file assets/sfx/accent/scratch-ping.wav.
```

`render.music_bed` builds the bed with `track.path.resolve()`.
`media/audio.plan_sfx` built each `SfxCue` with `track.path` — **unresolved**.
`Track.path` inherits the relativity of `settings.sfx_dir`, and both the shipped
`config.example.yaml` (`sfx_dir: ./assets/sfx`) and the `Settings` default
(`Path("assets/sfx")`) are relative to the user's working directory. Every render
runs FFmpeg with `cwd` set to the **project** folder — M0's colon rule — so the
library-relative path was written straight into `-i` and resolved against the
wrong directory.

Consequences before the fix:

* Any user who drops a single file into `assets/sfx/` and keeps the shipped
  config **cannot render at all**. Not a degradation: a non-zero exit.
* It could not have been caught by the existing tests. Every SFX fixture in
  `tests/unit/test_sfx.py` uses `/library/sfx/...` and
  `tests/integration/test_render_sfx.py` uses `tmp_path` — **absolute paths on
  both sides**, so the one thing that breaks is the one thing never exercised.
* The bed escaped because it was given `.resolve()` when it was written; the
  effects, added a task later, were not.

The fix is `track.path.resolve()` in `plan_sfx`, at the point of construction,
mirroring `music_bed`. It changes no fingerprint: `SfxCue.knobs()` hashes the
track *key*, not the path, and `render_hash` hashes the file's *content*, so no
existing project re-renders.

`test_an_effects_input_path_survives_ffmpegs_project_cwd` guards it with a real
relative library under a `monkeypatch.chdir`. Mutation-tested: reverting the
`.resolve()` fails it with
`assets/sfx/transition/whoosh.wav would be read relative to the project folder`.

## 2. FIXED — the Short's captions overflow the frame and lose letters

**Not fixed in the verification commit.** It is a rendering-quality change that
moves every project's caption fingerprint, and it deserved a task with a
measurement, not a drive-by. Fixed in its own commit afterwards; what follows is
the original diagnosis, then the correction and the result.

Looked at, at full resolution, in `final_vertical.mp4`: `version number,
allowing` renders as `ersion number, allowin` — the leading *v* and the trailing
*g* are outside the 1080 px frame. `opened moments later` and `instantly
reachable but` are clipped at both edges too.

Measured across both projects, with DejaVu Sans Bold at the declared sizes:

| | vertical (96 px, 3 words, 1080 px frame) | wide (64 px, 5 words, 1920 px frame) |
|---|---|---|
| lines wider than the 960/1800 px text box | **26 of 45 (58 %)** and **21 of 48 (44 %)** | **0 of 58** and **0 of 55** |
| lines wider than the **frame** — letters lost | **19 of 45 (42 %)** and **12 of 48 (25 %)** | **0**, **0** |
| widest line | **1572 px in a 1080 px frame** | 1708 px in a 1920 px frame |

Confirmed in the rendered pixels: glyphs at x = 5 and x = 1079 in a 1080-wide
frame, against a declared 60 px margin.

The cause is one line in `media/ass.py:_header`:

```python
"WrapStyle: 2",
```

WrapStyle 2 is *no word wrapping* — libass will not break a long line, it lets it
run off the screen. Wide survives only by arithmetic: 5 words at 64 px happen to
fit 1800 px. Vertical does not: 3 words at 96 px routinely need 1200–1600 px and
have 960.

Two things are wrong and both want fixing together:

* **`WrapStyle: 2` should be `0`** (smart wrapping, wider top line), so a long
  chunk becomes two lines instead of leaving the frame.
* **The horizontal margins are hardcoded `60,60` in `_style_line`** for both
  aspects, and so are the only part of the caption layout *not* authored per
  aspect. Spec 4.5 says neither aspect is derived from the other; the margins
  quietly are. They belong in `CaptionStyle` beside `margin_v`.

Note that `pipeline/thumbnail.py` already got this right —
`test_the_headline_wraps_on_words_rather_than_overflowing` exists, and the
thumbnails in this run wrap onto two and three lines correctly. The captions
never got the equivalent test.

A fix re-renders every project's captions and therefore every finished video. It
is worth it.

### The fix, and a correction to the numbers above

**The percentages above are over-stated by about a sixth, and here is why.** They
were measured with a text renderer told "size 96". ASS `Fontsize` is a *line
height*, not an em size: libass scales the face so that its ascender plus its
descender comes to `Fontsize`, which for DejaVu Sans is 0.859 em. Re-measured in
the units libass actually draws in, over all three projects that had vertical
captions:

| | measured here | as reported above |
|---|---|---|
| lines wider than the 960 px text box | **50 of 185 (27 %)** | 58 % and 44 % |
| lines wider than the **frame** — letters lost | **27 of 185 (15 %)** | 42 % and 25 % |
| widest line | **1670 px in a 1080 px frame** | 1572 px |

The defect itself is exactly as described, and confirmed in the shipped pixels:
burning all 185 captions through real libass puts ink at x = 0 and x = 1079 in a
1080-wide frame, on 26 of them. Wide was clean — 0 of 221 lines off the frame —
though one line was already past its declared text box at 1832 px.

Three changes, all in `media/ass.py`:

* **`WrapStyle: 2` → `WRAP_STYLE = 0`**, so libass breaks a long chunk instead of
  running it off screen. It is named in `captions.aspect_hash` too: a layout knob
  the cache cannot see is a stale artefact the cache swears is fresh.
* **`margin_l` / `margin_r` on `CaptionStyle`.** Wide keeps the 60 the writer used
  to hardcode, so not one wide caption moves. Vertical is authored at 72 — a
  fifteenth of a 1080-wide frame, which after the 96 px face's 6 px rim and 2 px
  shadow leaves 64 px of clear edge.
* **A break opportunity after an em or en dash that joins two words.** libass
  breaks at a space and at nothing else — not at a dash, not at U+200B, not at a
  soft hyphen, all three measured — so `representations—plain` is one unbreakable
  1055 px token in a 936 px box that no wrap mode can rescue. Without this the
  count does not reach zero.

Result, burning every caption of all four caption-bearing projects through real
libass:

| | vertical | wide |
|---|---|---|
| before — ink within 6 px of a frame edge | **26 of 185** | 0 of 221 |
| before — ink past the declared margin | **42 of 185** | 1 of 221 |
| after — ink within 6 px of a frame edge | **0 of 275** | **0 of 221** |
| after — ink past the declared margin | **0 of 275** | **0 of 221** |

(The vertical "after" set is larger because `how-to-sync-your-apple-devices` had no
vertical captions written yet.) Vertical ink now spans x = 71…1006 of 1080. The one
wide caption that had been past its margin — ink at x = 43 and x = 1877 against a
60 px margin — now wraps and sits at x = 266…1649.

Wide is otherwise untouched, and demonstrably so: re-rendering
`what-the-cloud-actually-means-for-your-documents` produces a `final_wide.mp4` that
is **bit-for-bit identical** to the one on disk — SSIM 1.000000 on all 3400 frames
and the same decoded-video-stream MD5. Its `.ass` differs by exactly one byte
(`WrapStyle: 2` → `0`); the style line, side margins included, is unchanged.

`captions:{wide,vertical}` and `render:{wide,vertical}` moved, once, deliberately;
`script`, `voice`, `align`, `visuals` and `assemble` did not, so the re-render costs
an encode and no provider quota.

## 3. OPEN — a lost stage cache silently rewrites the script

Not a crash, and arguably correct, but it surprised this verification and will
surprise a user.

Deleting `cache/stages.json` from the copied project re-ran `script`. It
completed in **0.73 s and spent no quota** — a response-cache hit — and returned
**completely different narration for all ten scenes**, with different visual
queries, because the cache entry it hit was written by M3's prompt (Task 12) and
the project on disk had been written by M1/M2's. Ten scenes of narration the
owner had reviewed and approved at gate 1 were replaced without a word.

The design is defensible: the prompt is *not* in the stage fingerprint, which is
exactly what stops an upgrade from re-scripting all ten of the owner's projects.
The gap is that the stage cache is then the **only** thing protecting approved
narration, and it lives in a directory `clean` is documented to leave alone but a
user might reasonably delete. Worth either (a) refusing to re-run `script` on a
project whose gate 1 is approved and whose scenes are non-empty, or (b) saying
loudly what it is about to overwrite.

## 4. OPEN — `crop_focus_x` re-encodes the wide segment it cannot change

Verified by mtime diff: changing one scene's `crop_focus_x` rebuilds that scene's
segment in **both** aspects, then both aspects' concat and joined video, then
both renders.

For a **video** asset that is wasted work. `_motion_filters` forces
`Motion.NONE` when the asset is a video, and `reframe_filter` returns `[]` for a
landscape spec — so `crop_focus_x` provably cannot alter a wide frame built from
video footage, and every chosen shot in both projects is video. For a **still**
it genuinely can, through `_pan_travel`.

The cost lands exactly where the owner will feel it: dragging the Short's crop
slider at gate 2 re-encodes the long video too. The fingerprint is conservative
rather than wrong; the refinement is to leave `crop_focus_x` out of the wide
scene hash when `_is_video_asset(scene)`.

## 5. OPEN, minor — free-tier headroom badges overflow their pill

Cosmetic, seen in the gate 2 screenshot at a 1280 px viewport: the headroom
badges (`9000 OF 9000 NEURONS LEFT TODAY`, `28 OF 28 REQUESTS LEFT THIS MINUTE`)
render text past their own rounded border and past the card's right edge. The
numbers are readable; the pill is not doing its job. A `text-overflow`/wrap or a
shorter label.

## 6. NOT A DEFECT, but unmeasurable as shipped — the SFX layer's loudness

The placement is right. With the same bed and the same footage, toggling
`sfx_enabled` moves the 3.2–6 kHz band at **every one of the nine cuts** in the
same direction (+0.08 to +0.96 dB, median +0.33 dB), while a control window with
no cue in it moves −0.04 dB. So the cues reach the output at the timestamps
`plan_sfx` computed, and the switch does what it says.

But +0.33 dB is below the threshold at which a level change is noticeable. That
is almost certainly the *fixture*, not the code: the whoosh is one this
verification synthesised at **−33.4 dBFS RMS**, and the `subtle` profile then
attenuates transitions by a further **14 dB**. A real, mastered whoosh sits
20-plus dB higher. **Whether the SFX layer is audible with a real library is
untested and untestable here**, because the project ships no audio — which is the
licensing decision working as designed, and a permanent limit on what this
particular measurement can say.

---

# Follow-ups

## Carried forward, re-verified as still accurate

### 7. Visual relevance is the big open problem

3.4/10 against an 8/10 target. **The starting point is
`docs/visual-relevance-baseline.md`, not `docs/visual-search-design.md`** — the
design document proposed better instructions, and the measurement showed
instructions are not the lever. Its four ranked recommendations stand: stop
generating queries blind (search, look, re-query, or restrict the model to a
vocabulary), prefer a sense-checked query to a specific one, accept that software
UI has no stock footage and needs a generated image or a screen recording, and do
not move the goalpost.

### 8. The vision re-rank works, is off by default, and is unmeasured

`visual_rerank_enabled` is `False` in `Settings` and in `config.example.yaml`.
Task 13's three defects are fixed — a live probe returns four well-formed scores
on `gemini-3.6-flash`, a 404 retires a stale model id and re-resolves, a
truncated reply is rejected by name, and a 404 no longer books quota. **What is
still unknown is whether it earns its quota**:
`test_vision_rerank_repaired_probe` has never run, because on 2026-08-31 the
budget it needed had been spent by the broken arm. It could run now — the day's
240 Gemini requests are untouched — and until it does, the switch stays off.

### 9. A docs sweep is owed on M1 and M2

`spike-results-m1.md` §2 and `spike-results-m2.md` §7 both describe
`QuotaTracker` as using a sliding 24-hour window. It does not, since Task 17:
`_SLIDING_WINDOWS` deliberately excludes `per_day`, and `_utc_day_start` resets
daily budgets on a UTC calendar boundary. `spike-results-m2.md` §4 describes
`encode_progress` substituting a module symbol; it does not, since Task 18:
`run_pipeline` takes a real `on_encode`, `run_render` takes a real `on_progress`,
and `test_web_render_gate.py` asserts the string `encode_progress` is absent from
the source. **Both sections are now historically wrong** and should be marked
resolved rather than left to mislead the next reader.

### 10. The repo-root `templates/` is still not packaged into a wheel — M6

`pyproject.toml` has `packages = ["src/videomaker"]` and nothing else.
`templates/tech_explainer.yaml` and `templates/_schema.md` live at the repo root
and ship in no wheel. Unchanged since M1 follow-up 4; still deferred to M6.

### 11. `web/media.py` still has a TOCTOU window

`_resolve_within` (line 62) resolves and rejects escapes, line 84 checks
`is_file()`, line 122 hands the path to `FileResponse`. The window between the
check and the open is unchanged. It only matters once the server is multi-user,
which is the same moment auth stops being optional. Still deferred.

### 12. Ambient SFX is scanned but never placed

`audio.SFX_ROLES` includes `"ambient"`, so `assets/sfx/ambient/` is walked,
indexed and reported by `videomaker music list`. Nothing places it:
`BEAT_ROLES` maps beats onto `accent` and `riser` only, `ROLE_LEAD_S` and every
entry in `SFX_PROFILES` name `transition`, `accent` and `riser`. A user who fills
`assets/sfx/ambient/` gets a directory listing and silence.

### 13. `preview.py` still encodes with libx264, deliberately

`PRESET = "ultrafast"` and a literal `"libx264"`, outside `stage_encoder`'s
reach and outside `STAGE_ORDER`. That is the right call for a throwaway 480p
proxy — the hardware path's win is in bitrate-heavy full-size encodes, and the
preview is neither — but it does mean `--fast` does not speed up the gate 3
proxy, which is the one wait a reviewer sits through most often. Worth a sentence
in the docs rather than a change.

## What did not need changing

* **Wide fingerprints did not move.** M3's riskiest change was adding vertical
  units to `STAGE_UNITS`; the copied project's ten wide segments were skipped on
  every re-run that touched only vertical inputs, and the guard held under a real
  `crop_focus_x` edit as well as under the plan's hash-stability test.
* **`videomaker clean`** reclaimed 152.5 MB of `build/` from one project, printed
  exactly what it would delete before doing it, left `output/`, `scenes/`,
  `project.json` and the stage ledger alone, and left the derived status
  correctly reading `assemble: pending`.
* **The response cache and quota ledger held across every one of the fifteen
  renders in this verification**, which is what made eleven measurement renders
  cost nothing.
* **The empty-library default is still exactly M1's render.** Every claim above
  about music and effects required files that had to be synthesised first; a
  fresh clone renders narration only, at −14.8 LUFS through a single loudness
  pass, on M1's argument list.
