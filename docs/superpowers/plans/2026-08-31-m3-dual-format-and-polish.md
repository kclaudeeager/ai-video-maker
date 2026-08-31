# M3 — Dual Format + Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One project produces both `final_wide.mp4` and a ≤3-minute `final_vertical.mp4` plus a thumbnail; captions are styled per aspect and never scaled from the other; music ducks audibly under narration; and the stock footage the pipeline picks on its own is measurably better than M1's 6/10.

**Architecture:** Everything stays inside M1's stage/cache model. Vertical is a second `Aspect` flowing through the same `captions → assemble → render` stages, **re-cropped from source assets** rather than cropped from the finished wide render. Audio is one extra `amix` input in the existing render pass. Visual search quality is confined to the `script` and `visuals` stages. No new pipeline stage is added except `thumbnail`.

**Tech Stack:** Everything from M0–M2, plus `Pillow` (thumbnails only).

## Global Constraints

- Everything from M0, M1 and M2 still holds: **$0 cost**, **Python `>=3.12,<3.13`**, **Linux x86_64 primary**, **no PyTorch**, **no MoviePy**, **AGPL-3.0-only**, **sign off every commit** (`git commit -s`), working directory is the repo root.
- **The tool ships no audio files.** See `docs/audio-design.md` — this is a licensing decision as much as an architectural one, and it is binding.
- **Vertical is never a crop of the finished wide render.** Each scene re-crops from its source asset. A pipeline that scaled the wide output would burn wide-styled captions into a 9:16 frame.
- **Never scale one aspect's caption style from the other.** Wide and vertical layouts are authored independently (spec 4.5).
- Out of scope: the remaining niche templates and `templates lint` (**M4**), metadata and upload (**M5**), Docker (**M6**).

### Read these first

- `docs/audio-design.md` — the music/SFX design, agreed with the owner. Layer ranking, the four levels of user control, and the explicit non-goals (no ripping audio from YouTube videos, no trending audio).
- `docs/visual-search-design.md` — the visual search workstream, including the requirement that it produce a **measured** before/after.
- `docs/superpowers/spike-results-m1.md` and `-m2.md` — the open follow-ups, several of which are tasks here.

### What M1/M2 give you (verified — do not re-derive)

- `models.py` — `Aspect` (`WIDE`/`VERTICAL` both already exist), `Scene.in_short` (exists, defaults `True`, currently ignored), `SceneVisual.crop_focus_x` (exists, `0.0-1.0`, currently ignored), `OutputSpec` keyed by aspect, `AssetRef`, `StockResult` (**has** `preview_url`; `AssetRef` does **not** — see Task 2).
- `pipeline/assemble.py` — `VideoSpec`, `SPECS` (wide only), `build_scene_filter(scene, spec, *, gap_s)` (pure, string-returning), `scene_timeline`, `segment_relpath`, `narration_relpath`, `video_relpath`, `_motion_filters`, `_cover_scale`, `_pan_travel`.
- `pipeline/render.py` — `_render_args(root, aspect, *, burn_captions)`, `subtitles_filter`, `output_relpath`, `run_render`. Runs FFmpeg with `cwd=<project>` and **relative** paths (M0 finding — an absolute `.ass` path with a colon breaks the filter parser).
- `media/ass.py` — `CaptionStyle`, `STYLES` (wide only), `chunk_words`, `chunk_grouped`, `merge_degenerate_chunks`, `write_ass`, `ass_colour` (**BGR**, not RGB — unit-tested), `CHUNK_INSET_S`, `MIN_DISPLAY_DURATION_S`.
- `pipeline/base.py` — `StageDeps`, `StageResult`, `SCENE_GAP_S = 0.5`, `call_chain`, `relative_to_project`.
- `runner.py` — `STAGE_ORDER`, `STAGE_RUNNERS`, `STAGE_UNITS`, `STATUS_AFTER`, `GATE_BEFORE`, `derive_status`, `run_pipeline`, `build_deps`.
- `preview.py` — `build_preview`, cached at `preview:wide`, deliberately outside `STAGE_ORDER`.
- `web/` — five pages, 24 routes, one worker thread, `JobProgress`.
- `providers/ratelimit.py` — `QuotaTracker`, `SOFT_BUDGETS`, `Budget`.

### Plan conventions

Same as M1 and M2: **Interfaces blocks are normative**, **test code given in full where it defines behaviour**, implementation prose except where subtle. Every task ends `uv run pytest -q && uv run ruff check .` green. Mutation-test any guarantee a test claims to hold — every M1/M2 task did, and it caught real vacuous tests each time.

Ruff flags well beyond `E4,E7,E9,F` here: `C408`, `UP017`, `B008` (per-file-ignored under `src/videomaker/web/*`), `F401`, `RUF`, `ISC`, `TRY004`, `FURB`, `UP047`.

---

## Phases

M3 is the largest milestone. It is arranged so it can be stopped cleanly at a phase boundary.

| phase | tasks | ends with |
|---|---|---|
| **A — Vertical** | 1–7 | **The personal MVP: a Short from the same project.** Stop here if you want to start publishing. |
| **B — Audio** | 8–11 | Music beds, ducking, transition SFX, the gate-3 picker |
| **C — Visual search** | 12–14 | A measured improvement on M1's 6/10 |
| **D — Polish** | 15–19 | Thumbnails, `clean`, hardware fast render, the carried fixes |

Phase A alone satisfies the owner's stated MVP bar. Phases B–D are ordered by value, not by dependency.

---

# Phase A — Vertical

### Task 1: Vertical spec, per-aspect plumbing and the `in_short` subset

**Files:** modify `pipeline/assemble.py` (`SPECS`), `runner.py` (per-aspect units); test `tests/unit/test_vertical_spec.py`

**Interfaces:**
- `SPECS[Aspect.VERTICAL] = VideoSpec(width=1080, height=1920, fps=30)`; `VERTICAL_SPEC`.
- `timeline_scene_ids(project, aspect) -> list[str]` — wide is every assemblable scene; vertical is the `in_short` subset, in the same order.
- `MAX_SHORT_S = 180.0`; `short_fits(project) -> bool` and the duration of the `in_short` subset.
- `STAGE_UNITS` for `captions`, `assemble` and `render` gain a unit per aspect (`captions:vertical`, `assemble:vertical:sNN`, `render:vertical`).

**The trap:** M1's `_captions_units`/`_assemble_units`/`_render_units` hard-code wide. Adding vertical must not change any **wide** hash — otherwise every existing project re-renders from scratch on upgrade. Assert that explicitly: build a project, record every wide unit hash before and after the change, and require them byte-identical.

- [x] **Step 1: Write the failing tests** (including the wide-hash-stability test above)
- [x] **Step 2: Run to confirm fail, implement, confirm pass**
- [x] **Step 3: Mutation-test the hash stability, then commit**

```bash
git commit -s -m "feat: vertical video spec, per-aspect stage units, in_short subset"
```

---

### Task 2: `AssetRef.preview_url` (carried follow-up)

**Files:** modify `models.py`, `providers/stock/pexels.py`, `providers/image/cloudflare.py`, `providers/mock.py`, `web/templates/_scene_card.html`; tests as needed

`StockResult` already carries `preview_url`; `AssetRef` discards it on persist, which is why M2's storyboard shows grey `CLIP · 1920×1080 · 25s` placeholders for un-downloaded alternatives (visible in the M2 DoD screenshots). Carry it through.

**This is a prerequisite for both** the storyboard thumbnails **and** the optional Gemini visual re-rank in Task 13 — do it once, here.

**Backward compatibility:** `project.json` files written before this change have no `preview_url`. Give it a default so existing projects load; assert that with a fixture of the pre-change shape.

- [ ] **Step 1–3: TDD, then commit**

```bash
git commit -s -m "feat: carry preview_url onto AssetRef so candidates show real thumbnails"
```

---

### Task 3: Vertical caption style and karaoke word-pop

**Files:** modify `media/ass.py`; test `tests/unit/test_ass_vertical.py`, extend `test_ass_writer.py`

**Interfaces:**
- `STYLES[Aspect.VERTICAL] = CaptionStyle(font_size=96, words_per_chunk=3, ...)` centred at ~62% height (spec 4.5). **Authored, not derived** — no arithmetic on the wide numbers.
- `write_ass(..., karaoke: bool = False)` — per-word `Dialogue` events with the active word recoloured.

**Two traps, both already burned once in this codebase:**
- **ASS colours are BGR.** `ass_colour` is correct and tested; any new colour constant must go through it. A karaoke highlight written as RGB will silently render the wrong colour.
- **Whisper timings are zero-gap** and can be degenerate (`start == end == 0.0` — M1 shipped a zero-duration caption from exactly this). Karaoke multiplies the number of events, so re-check `merge_degenerate_chunks` still holds and no per-word event has `end <= start`.

- [ ] **Step 1–3: TDD (assert BGR explicitly for the highlight colour), then commit**

```bash
git commit -s -m "feat: vertical caption style and karaoke word-pop captions"
```

---

### Task 4: Vertical assembly — re-crop from source

**Files:** modify `pipeline/assemble.py`; test `tests/unit/test_assemble_vertical.py`

**Interfaces:** `build_scene_filter(scene, VERTICAL_SPEC, gap_s=...)` produces a 1080×1920 graph that **re-crops the source asset**:
`crop='min(iw,ih*9/16)':ih:'(iw-ow)*crop_focus_x':0` then scale to 1080×1920 (spec 4.5). Template-selectable `blur_pad` style (blurred fill behind a fitted foreground) is an alternative, not the default.

- `crop_focus_x` finally does something: 0.0 = left edge, 1.0 = right edge, 0.5 = centre.
- Stills keep `Motion.PAN` over a 120% pre-scale; `Motion.ZOOM` pre-upscales 2× (M1 measured visible jitter without it).

**Test as strings** (pure `build_scene_filter`) plus one real render asserting 1080×1920 via ffprobe. Assert scale-before-crop ordering, as the wide tests do.

**Guard:** vertical segments must have their own cache unit — re-cropping scene 3 for vertical must not invalidate scene 3's wide segment. Prove it by counting `run_ffmpeg` calls, the way M1 and M2 did.

- [ ] **Step 1–3: TDD, mutation-test the per-aspect cache scoping, then commit**

```bash
git commit -s -m "feat: vertical assembly re-cropping each scene from its source asset"
```

---

### Task 5: Vertical narration track and timeline

**Files:** modify `pipeline/assemble.py`; test extend `test_assemble_vertical.py`

The vertical voice track is a **separate concatenation of the same per-scene wavs** over the `in_short` subset (spec 4.5) — not a re-synthesis. No TTS call may happen here; assert zero TTS calls.

Captions offsets for vertical must be computed from the vertical timeline, not the wide one. This is the same class of bug M2 Task 12 hit with the render progress bar: a duration measured against the wrong timeline. Test that scene N's vertical caption offset equals the sum of *preceding in_short scenes* plus gaps.

- [ ] **Step 1–3: TDD, then commit**

```bash
git commit -s -m "feat: vertical narration track over the in_short subset"
```

---

### Task 6: Vertical render and the ≤3-minute rule

**Files:** modify `pipeline/render.py`, `runner.py`; test `tests/integration/test_render_vertical.py`

`run_render` renders every aspect in `project.outputs`. `final_vertical.mp4` must be **≤ `MAX_SHORT_S`**; when the `in_short` subset exceeds it, fail with a clear, actionable message naming which scenes to untick — do not silently truncate mid-sentence.

Integration test with real FFmpeg: 1080×1920, h264+aac, duration matching the in_short timeline, captions burned with the **vertical** style.

- [ ] **Step 1–3: TDD, then commit**

```bash
git commit -s -m "feat: vertical render with the three-minute Shorts limit enforced"
```

---

### Task 7: Gate 2 vertical controls — crop slider and `in_short`

**Files:** modify `web/routes/storyboard.py`, `web/templates/_scene_card.html`, `web/static/style.css`; test `tests/unit/test_web_vertical_controls.py`

- A crop-focus slider per scene with a **9:16 overlay** on the clip, so the user sees what the Short will keep.
- An `in_short` toggle per scene, showing the running vertical duration against the 3-minute limit.

Follow M2's established patterns exactly: compare-first-**then**-lock (a no-op must not even take the `flock` — M2 Task 9's mutant 5), htmx-vs-plain-form dual response, out-of-band `#gate-state` swap.

**Verify in a real browser** with the browser-automation skill and screenshot it; the slider is a visual feature and `TestClient` runs no JavaScript.

- [ ] **Step 1–3: TDD, browser-verify, then commit**

```bash
git commit -s -m "feat: crop-focus slider with 9:16 overlay and in_short toggle"
```

**→ Phase A ends here. At this point one project yields both a wide video and a Short. This is the owner's stated MVP bar.**

---

# Phase B — Audio

### Task 8: Music and SFX library, index, and `videomaker music`

**Files:** create `src/videomaker/audio.py`, `assets/music/README.md`, `assets/sfx/README.md`, `assets/music/library.yaml` (example); modify `cli.py`; test `tests/unit/test_audio_library.py`

Implements `docs/audio-design.md`. Layout `assets/music/<mood>/` and `assets/sfx/<role>/` (`transition`, `accent`, `riser`, `ambient`). `library.yaml` records title/artist/licence/attribution/source per file — **that record is what lets M5 generate a correct attribution block**, the same way Pexels attribution already flows from `AssetRef.attribution`.

`videomaker music scan` indexes (ffprobe for duration) into `~/.cache/ai-video-maker/music_index.json`; `music list` prints it. The index is a cache, never the source of truth.

**An empty library is not an error** — the pipeline renders narration-only exactly as it does today. Test that path first; it is the default for every new clone.

The READMEs carry the source table from the design doc (YouTube Audio Library, Pixabay, Freesound CC0, FMA, Incompetech) and the explicit non-goal: **never extract audio from arbitrary YouTube videos.**

- [ ] **Step 1–3: TDD, then commit**

```bash
git commit -s -m "feat: user-owned music and SFX library with a licence record"
```

---

### Task 9: Mixing — music bed, ducking, two-pass loudness

**Files:** modify `pipeline/render.py`, `media/audio.py` (new); test `tests/unit/test_audio_mix.py`, `tests/integration/test_render_music.py`

`sidechaincompress` ducking + `amix normalize=0` + `loudnorm` (spec 4.7).

**Two measured facts from M1 that bite here:**
- The audio chain is **24 kHz mono end to end** (Kokoro's native rate). Music is 44.1/48 kHz stereo. **Resample at mix time, not at TTS time** — M1 follow-up 5, and this is the feature that makes it matter.
- Single-pass `loudnorm` measured **−14.9 LUFS against a −14 target**. Adding music makes that ~1 LU miss more visible; switch to **two-pass** `loudnorm` here (M1 follow-up 11).

**Prove the duck audibly, not structurally.** A test asserting the filter string contains `sidechaincompress` proves nothing about the mix. Measure: render a clip with music under narration, then measure music-band loudness during speech versus during a gap (ffmpeg `astats`/`ebur128`), and assert a real reduction.

- [ ] **Step 1–3: TDD with a measured duck, then commit**

```bash
git commit -s -m "feat: music bed with sidechain ducking and two-pass loudness"
```

---

### Task 10: Transition SFX and beat-mapped accents

**Files:** modify `pipeline/render.py`, `media/audio.py`, `templates/tech_explainer.yaml`; test `tests/unit/test_sfx.py`

Per `docs/audio-design.md`, in its stated priority order: **transition SFX on scene cuts first** (every cut timestamp is already known from `scene_timeline` — highest value per unit of work), then **beat-mapped accents** using the template's existing `structure: [hook, context, mechanism, implication, close]`, which is already an editorial map. No LLM call, no inference from footage.

Template gains `sfx_profile: subtle | punchy | none`. **Literal foley is explicitly out of scope** — the design doc explains why (Pexels clips arrive mute, and a foley hit slightly out of sync reads worse than silence).

- [ ] **Step 1–3: TDD, then commit**

```bash
git commit -s -m "feat: transition SFX on cuts and beat-mapped accents"
```

---

### Task 11: Gate 3 — music picker and side-by-side previews

**Files:** modify `web/routes/render.py`, `web/templates/preview.html`; test extend `test_web_render_gate.py`

The spec's gate-3 route: pick a track, preview the mix, toggle SFX, adjust ducking; **wide and vertical previews side by side.** Needs a vertical 480p preview alongside the existing wide one (extend `preview.py`, still outside `STAGE_ORDER`).

Browser-verify and screenshot.

- [ ] **Step 1–3: TDD, browser-verify, then commit**

```bash
git commit -s -m "feat: gate 3 music picker and side-by-side aspect previews"
```

---

# Phase C — Visual search quality

### Task 12: Multiple queries per scene, laddering, and metadata ranking

**Files:** modify `pipeline/script.py`, `pipeline/visuals.py`, `templates/tech_explainer.yaml`; test `tests/unit/test_visual_ranking.py`

Implements items 1–3 and 5 of `docs/visual-search-design.md`: 2–3 ordered queries per scene from the same LLM call; few-shot good/bad examples in the template prompt; a fallback ladder capped at 3 attempts; a pure ranking function over tag overlap, duration headroom and resolution; a small cliché denylist.

**Measure the baseline BEFORE changing anything** (Task 14 is the harness) so the improvement is provable rather than asserted.

- [ ] **Step 1–3: TDD, then commit**

```bash
git commit -s -m "feat: multiple visual queries per scene with laddering and ranking"
```

---

### Task 13: Optional Gemini Flash visual re-rank

**Files:** create `providers/vision/gemini.py`; modify `pipeline/visuals.py`, `config.py`; test `tests/unit/test_visual_rerank.py`

Item 4 of the design doc. Gemini Flash's free tier accepts images (1,500 req/day), so scoring candidate thumbnails against the scene narration is available at $0. Needs `AssetRef.preview_url` from Task 2.

**Budget honestly and gate it:** 10 scenes × 4 candidates = 40 images per video ≈ 37 videos/day at the cap. Fire **only when the metadata score is ambiguous**, make it configurable, and default it **off** until Task 14 shows it earns its quota. Record spend through `QuotaTracker` like every other provider — and note Task 17 fixes the ledger persistence this depends on.

- [ ] **Step 1–3: TDD, then commit**

```bash
git commit -s -m "feat: optional Gemini Flash visual re-rank for stock candidates"
```

---

### Task 14: Relevance harness — the before/after number

**Files:** create `tests/quality/test_visual_relevance.py` (marked `slow`), `docs/visual-relevance-baseline.md`

**The design doc's binding condition: the work is not done without a measured before/after.** Run N fixed topics, capture each chosen clip with its scene narration, and score relevance — by hand once, or with an LLM judge over narration plus the clip's tags and alt text.

Record M1's measured **6/10 strongly on topic** as the baseline and state the target (**8/10**). Report the number Tasks 12–13 actually achieved, including if it did not reach the target — an honest miss is more useful than a moved goalpost.

- [ ] **Step 1–3: Measure, record, commit**

```bash
git commit -s -m "test: visual relevance harness with a measured before/after"
```

---

# Phase D — Polish and carried fixes

### Task 15: Thumbnails

**Files:** create `pipeline/thumbnail.py`, `assets/fonts/` (2 OFL fonts + `OFL.txt`); modify `runner.py` (new `thumbnail` stage), `NOTICE.md`; test `tests/unit/test_thumbnail.py`

Pillow: extract a frame (or use an AI image) → 1280×720, gradient scrim, auto-fit OFL display font (spec 4.5). **Bundling fonts changes the licensing footprint — record them in `NOTICE.md`** with version and licence, as M2 did for htmx.

This also fills the gap M1 left: `render.py`'s `fontsdir` is omitted today because `assets/fonts/` does not exist. Once it does, confirm the render picks it up and that captions still render identically.

Adding a `thumbnail` stage **does** change `STAGE_ORDER` — unlike M2's preview, this is a real deliverable. Verify `derive_status`, the golden-path tests and the web stepper all still agree.

- [ ] **Step 1–3: TDD, then commit**

```bash
git commit -s -m "feat: thumbnail stage with bundled OFL fonts"
```

---

### Task 16: `videomaker clean`

**Files:** modify `cli.py`; create `src/videomaker/cleanup.py`; test `tests/unit/test_clean.py`

`clean [--keep-outputs] [--all] <project|--everything>` removes `build/` intermediates and, optionally, cached assets. M1 measured 1–3 GB of intermediates per project and `doctor` warns under 20 GB free.

**Deleting files is the one irreversible thing in this codebase.** Require confirmation unless `--yes`; print exactly what will go and its size first; never touch `output/` unless explicitly asked; and test that `--keep-outputs` genuinely keeps them.

- [ ] **Step 1–3: TDD, then commit**

```bash
git commit -s -m "feat: clean command for build intermediates"
```

---

### Task 17: Quota ledger — calendar-day reset

**Files:** modify `providers/ratelimit.py`; test extend `test_ratelimit.py`

M1 follow-up: `_prune` uses `clock() - 86400`, a **sliding** 24-hour window, but the real APIs reset on a calendar day. Spending Gemini's 240 at 21:00 currently blocks until 21:00 tomorrow rather than resetting at midnight UTC.

Switch `per_day` to a calendar-day boundary (UTC, and say so — Gemini resets Pacific, Cloudflare UTC; document the discrepancy rather than pretending it does not exist). Keep `rpm` and `per_hour` sliding.

M2's fix to persist the LLM ledger is a prerequisite and already landed; verify it still holds.

- [ ] **Step 1–3: TDD, then commit**

```bash
git commit -s -m "fix: reset per-day quota on a calendar boundary, not a sliding window"
```

---

### Task 18: Hardware fast-render, and `run_render`'s real progress parameter

**Files:** modify `pipeline/render.py`, `pipeline/assemble.py`, `config.py`, `doctor.py`, `web/routes/render.py`; test `tests/unit/test_fast_render.py`

Two carried follow-ups that touch the same code:

1. **Hardware encode.** `doctor` currently says `h264_qsv detected; renders use libx264 (CPU) until fast-render mode lands in M3` — this is that. Add `--fast` / `render.fast_mode` selecting `caps.hw_encoder` (QSV → VA-API → VideoToolbox), with libx264 the default quality path. Encoding was **70 s of M1's 184 s** cold run. Update doctor's wording once it is true.
2. **`run_render` gains a real `on_progress` parameter.** M2 Task 12 instruments FFmpeg by *symbol substitution*, safe only because one worker runs one job. Do this **before** anything adds a second worker, not after.

- [ ] **Step 1–3: TDD, then commit**

```bash
git commit -s -m "feat: hardware fast-render mode and a real on_progress on run_render"
```

---

### Task 19: M3 definition-of-done verification

**Files:** create `docs/superpowers/spike-results-m3.md`

- [ ] **Step 1: Real battery, real providers, real browser**

One project → `final_wide.mp4` + `final_vertical.mp4` (≤3 min) + `thumbnail.jpg`. Captions styled per aspect. **Music ducks audibly** — verify by measurement, and by listening if the owner can.

- [ ] **Step 2: Watch both outputs.** Extract frames from each; confirm the vertical crop keeps the subject (that is what `crop_focus_x` is for) and that vertical captions are not wide captions scaled.
- [ ] **Step 3: Report the visual-relevance number** from Task 14 against the 6/10 baseline.
- [ ] **Step 4: Record and commit**

```bash
git commit -s -m "docs: M3 complete — dual format, audio, better visuals, polish"
```

---

## Self-review notes

- **Spec coverage (M3)**: vertical pipeline ✓ (T1, T4–T6), karaoke captions ✓ (T3), music ducking ✓ (T9), thumbnails ✓ (T15), `clean` ✓ (T16), hardware fast mode ✓ (T18). Beyond the spec, by the owner's explicit request: the audio design ✓ (T8–T11) and visual search quality ✓ (T12–T14).
- **Carried follow-ups closed here**: `AssetRef.preview_url` (T2), sliding-window quota (T17), `doctor`'s hardware-encoder claim and `run_render`'s progress parameter (T18), the missing `assets/fonts/` that made `fontsdir` unreachable (T15). Still deferred: the `media.py` TOCTOU (only matters once the server is multi-user, which is the same moment auth stops being optional) and the repo-root `templates/` packaging gap (M6).
- **The riskiest task is T1**, not the render work: adding vertical units to `STAGE_UNITS` could silently change every **wide** hash and re-render every existing project on upgrade. It carries an explicit before/after hash-stability test.
- **Phase A is independently shippable** and is the owner's stated MVP bar. B, C and D are ordered by value, not dependency — except T13, which needs T2, and T14, which measures T12–T13.
- **Two lessons from M1/M2 are written into the tasks rather than left to memory**: a duck asserted by grepping the filter string proves nothing (T9 measures it), and a relevance improvement asserted without a baseline is a vibe (T14 measures it).

---

### Task 20: Visual design pass across the whole UI

> **Sequencing: run this AFTER Task 7**, not at the end of M3. Task 7 adds the
> crop slider and `in_short` toggle to gate 2; restyling before those exist means
> bolting unstyled controls onto a finished design. It belongs to **Phase A** in
> priority even though it is numbered last — the owner considers it MVP-blocking.

**Files:** `web/static/style.css`, all `web/templates/*.html`, possibly a small
`web/static/app.js`; a bundled display font under `assets/fonts/` (shared with
Task 15's thumbnails — coordinate, and record it in `NOTICE.md` once)

**REQUIRED: load the `frontend-design` skill before writing any CSS.** This task
is about aesthetic direction and typography, not about making the existing
stylesheet bigger. Establish the direction first, then apply it.

**The owner's brief, verbatim:** *"there is still more work in UI/UX it has to be
simple to use and easy to understand the steps, should look appealing and also
how it looks should tell the user that they are on the right website"*

Three distinct requirements, and they need different work:

1. **Simple to use, steps easy to understand.** The pipeline is a seven-stage
   state machine behind three human gates, and the UI currently exposes that
   almost literally. A first-time user should understand *where they are*, *what
   they are being asked to judge*, and *what happens when they approve* — without
   reading the spec. Gate 2 is a very long scroll at five scenes and will be
   worse at ten; progressive disclosure matters more than more chrome.
2. **Appealing.** Real typography (not the system stack), a considered palette,
   deliberate spacing rhythm, and states that feel responsive. htmx swaps
   currently land with no transition, so the page appears to twitch.
3. **Identity — "tells the user they are on the right website."** A name, a mark,
   and a colour that are *this* product. It should read as a focused craftsman's
   tool for one creator, not enterprise SaaS and not a generic dashboard
   template. The tool is local-first and human-in-the-loop; the design should
   feel calm and confident rather than busy.

**Constraints that do not move:**
- **No npm, no build step, no CDN** — the same rule htmx was vendored under.
  Any font is committed to the repo with its licence recorded in `NOTICE.md`.
- Keep every route, form field name, `data-*` attribute and `hx-*` binding
  working. M2's tests assert on `data-gate`/`data-approved`, `hx-trigger`
  presence at non-terminal states, `select[name=voice]`, and the scene-row
  contract. **A redesign that breaks those is a regression, not a redesign** —
  the suite passing is the definition of "did not break it".
- Accessible by default: real labels, visible focus rings, adequate contrast
  (check it, do not eyeball it), and it must still work with JavaScript off.

**Method:**

- [ ] **Step 1: Direction before pixels.** Load `frontend-design`. Write a short
  direction note (name/mark, palette with contrast ratios, type scale, spacing
  rhythm, component inventory) into `docs/ui-design.md`. Commit that first, so
  the reasoning is reviewable separately from 800 lines of CSS.
- [ ] **Step 2: Apply it** across all five pages plus every partial.
- [ ] **Step 3: Prove nothing broke.** `uv run pytest -q` green — with special
  attention to `test_web_*`, which assert on markup contracts.
- [ ] **Step 4: Look at it.** Screenshot all five pages in a real browser at
  1280px **and at a narrow width**, and read the screenshots. Report console
  errors and contrast failures. The owner will judge from these, so capture real
  content, not empty states.
- [ ] **Step 5: Commit**

```bash
git commit -s -m "feat(web): visual design pass — identity, typography, clearer gate flow"
```

**Judgment call to make deliberately and state:** whether to commit to one
direction or offer the owner a choice. One coherent direction applied well is
usually more useful than three half-applied ones — but say which you chose and
why, and make the palette and type scale easy to retune from tokens at the top of
the stylesheet, because taste is the owner's call and iterating should be cheap.
