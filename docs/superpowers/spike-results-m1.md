# M1 Spike Results

Definition-of-done verification for M1 (CLI happy path, wide). Run 2026-08-30 on
the same machine as M0: HP ProBook 450 G10 — i7-1355U (12 threads), 16GB RAM,
Zorin OS 18.1 (Ubuntu 24.04 base), x86_64, Python 3.12.12 via uv,
ffmpeg 6.1.1-3ubuntu5 with libass.

**Everything below is measured with real providers.** Groq, Pexels, Kokoro and
faster-whisper all made real calls; no mock was used anywhere in this run.

## Definition of done

`videomaker new "how ssds work" -t tech_explainer && videomaker run <id> --yes`
produced a playable `output/final_wide.mp4` with burned captions, and the
immediate re-run finished in **0.52 s** spending **zero** quota. DoD met.

```
$ uv run videomaker new "how ssds work" -t tech_explainer
created how-ssds-work in workspace/projects/how-ssds-work
next: videomaker run how-ssds-work --yes

$ uv run videomaker run how-ssds-work --yes
  ran script (0 unit(s) skipped)
  ran voice (0 unit(s) skipped)
  ran align (0 unit(s) skipped)
  ran visuals (0 unit(s) skipped)
  ran captions (0 unit(s) skipped)
  ran assemble (0 unit(s) skipped)
  ran render (0 unit(s) skipped)
status: rendered

$ time uv run videomaker run how-ssds-work --yes
  cached script (1 unit(s) skipped)
  cached voice (10 unit(s) skipped)
  cached align (10 unit(s) skipped)
  cached visuals (10 unit(s) skipped)
  cached captions (1 unit(s) skipped)
  cached assemble (11 unit(s) skipped)
  cached render (1 unit(s) skipped)
status: rendered
real    0m0.524s

$ uv run videomaker status how-ssds-work   # status: rendered, all 7 stages current
$ uv run pytest -q                          # 278 passed in 107.06s
$ uv run ruff check .                       # All checks passed!
$ uv run videomaker doctor                  # 8/8 OK, unchanged from before the run
```

## Wall times

| run | wall time |
| --- | --- |
| cold (empty response cache, empty quota ledger) | **184.14 s** |
| warm run 1 (immediately after) | **0.87 s** |
| warm run 2 (`time`, page cache hot) | **0.52 s** |

Cold : warm ≈ **350×**. The 10 s DoD ceiling for a warm run has ~19× headroom.

### Per-stage cold timings

Measured by timestamping the CLI's own per-stage progress lines. Output video is
115.93 s long; narration is 111.00 s across 10 scenes.

| stage | wall | notes |
| --- | --- | --- |
| script | 5.63 s | includes ~0.5 s CLI import/startup; one Groq call |
| voice | 71.98 s | Kokoro, 10 scenes, 111.0 s of audio → **RTF 0.65** |
| align | 21.71 s | faster-whisper `base`/int8/CPU, 111.0 s audio → **RTF 0.196** incl. one model build |
| visuals | 13.66 s | 10 Pexels searches + 10 clip downloads (119 MB) |
| captions | < 0.01 s | pure string building, no I/O of consequence |
| assemble | 32.03 s | 10× 1080p30 libx264 `veryfast` CRF 18 segments + narration bed |
| render | 38.72 s | 116 s of 1080p30 libx264 + ASS burn-in + loudnorm → **3.0× realtime** |
| **total** | **184.14 s** | |

M0 finding 2 is confirmed and then some: building `WhisperModel` once put align at
RTF 0.196, five times better than M0's warm per-file RTF of 1.02. Per-scene
construction would have cost roughly realtime.

Warm-run stage timings are all ≤ 0.14 s; the whole warm run is dominated by Python
import time, not by cache checks.

## Quota actually consumed

`~/.cache/ai-video-maker/quota.json` after the cold run, byte-identical after both
warm runs (the objective proof of "no provider calls on a re-run"):

| provider | units spent | soft budget (published) | notes |
| --- | --- | --- | --- |
| Groq | **1 call** | 28 rpm (30) | one script call, no repair retry needed |
| Pexels | **10 requests** | 190/hour (200) | one search per scene; clip downloads are not metered |
| Cloudflare Workers AI | **0 neurons** | 9 000/day (10 000) | **Flux was never invoked** |
| Gemini | 0 | 240/day (250) | LLM fallback never reached |

Soft budgets from `SOFT_BUDGETS` in `providers/ratelimit.py`, deliberately set
under the published hard limits. Peak usage was 10/190 on the Pexels hourly
window; nothing came close to a refusal.

**Pexels satisfied all ten scenes, so the image provider never ran.** The whole
video cost one LLM call and ten stock searches. Cloudflare's neuron budget stayed
untouched, and M0's ~58-neurons-per-image measurement went unexercised in M1 — it
will only be load-bearing once a scene's stock search comes back empty.

Response cache: 11 entries, 420 KB (1 Groq completion + 10 Pexels searches).
Stage cache `cache/stages.json`: 78 entries — 44 stage fingerprints and 34
`status:`-prefixed ones (see follow-up 2).

Disk: project folder 301 MB (build 123 MB, scenes 119 MB, output 60 MB); shared
model cache 479 MB (kokoro 311 MB, voices 27 MB, whisper-base 142 MB).

## Resolved Groq model

**`openai/gpt-oss-120b`** — first entry of `GROQ_MODEL_PREFERENCE`, confirmed
present in the live catalogue at run time.

The live catalogue has 14 entries and **`allam-2-7b` still sorts first
alphabetically**, so M0 finding 1 remains a live hazard, not a historical one: a
naive "take the first id" resolver would still pick the 4k-context model that
produced M0's factually wrong script. The preference-intersection resolver in
`providers/llm/groq.py` is what stops that, and it raises `ProviderConfigError`
rather than substituting when the intersection is empty.

Also worth recording: `llama-3.3-70b-versatile` and `llama-3.1-8b-instant` are
still absent from the catalogue, as M0 found.

## Output inspection

`workspace/projects/how-ssds-work/output/final_wide.mp4`, 62 892 208 bytes.

```
video  h264 High, yuv420p, 1920x1080, 30/1 fps, 3478 frames, 4 145 kb/s, 115.933 s
audio  aac LC, 48000 Hz, stereo, 185 kb/s, 115.928 s
format mov,mp4,m4a  115.933 s, 4 340 kb/s overall
```

Duration checks out arithmetically: 111.00 s of narration + 10 × `SCENE_GAP_S`
(0.5 s) = 115.997 s of timeline, and the encode lands 2 frames short of that
because `-shortest` stops on the audio bed. `build/timeline_wide.json` agrees
scene for scene.

### Frames

Ten frames were extracted across the video and inspected, plus boundary frames at
three cuts. In every one the caption burned into the picture is **exactly** the
`Dialogue:` line the `.ass` file predicts for that timestamp.

| t | scene | what is on screen | caption |
| --- | --- | --- | --- |
| 6.0 s | s01 | hands typing on a backlit keyboard, purple bokeh | "data into existence. The screen" |
| 12.50 s | s01 (gap) | same keyboard shot, **no caption** — the 0.5 s tail | (none, as designed) |
| 12.57 s | s02 | opened HDD, mirror platter and actuator arm | "A solid‑state drive can read" |
| 18.0 s | s02 | same HDD, spindle spinning | "disk needs several milliseconds, a" |
| 23.0 s | s03 | different HDD, blue-lit platter, head arm visible | "rotating platters, so the read" |
| 27.0 s | s03 | same HDD, framing panned in | "head must wait for the" |
| 40.0 s | s04 | server rack with patch panels and NAS units | "and lost everything when the" |
| 52.0 s | s05 | blue-lit PCB macro, surface-mount components | "each cell trapping electrons to" |
| 84.0 s | s08 | lab-coated worker at storage shelving | "program‑erase cycles, manufacturers add extra" |
| 95.0 s | s09 | circuit-board macro, capacitors and slot | **(none)** — inter-chunk gap |
| 108.0 s | s10 | PC interior, RGB fan and RAM, purple lighting | "and durability while accepting limited" |

Captions render as 64 px DejaVu Sans bold, white with a 3 px black outline,
bottom-centre (`Alignment 2`, `MarginV 160`), legible against both the darkest
(s02, mean luma 46) and brightest (s08, mean luma 139) shots. No caption
overflowed the frame; `WrapStyle 2` kept every chunk on one line.

The 12.50 s and 95.0 s rows are the interesting ones: they are frames where the
`.ass` has no active dialogue, and the picture correctly shows none. That is M0
finding 3 working — the writer insets chunk boundaries so zero-gap whisper
timings do not produce edge-to-edge captions.

**Visuals differ per scene.** All ten scenes chose distinct Pexels clips
(source ids 8888432, 19285752, 3289546, 5028622, 6754824, 34645150, 30712308,
31522472, 6754830, 3147349), every source clip is longer than the segment it
fills (13.3–61.0 s source vs 9.9–14.7 s needed), so nothing was looped or held.

### No black frames at cuts

Two independent checks, both clean:

- `blackdetect=d=0.03:pic_th=0.98:pix_th=0.10` over the whole file reported
  **no black intervals at all**.
- Per-frame mean luma sampled across three cuts steps straight from one scene's
  level to the next with no dip toward zero:

  | cut | luma before → after |
  | --- | --- |
  | s01→s02 @ 12.53 s | 54.6 → 45.6 |
  | s04→s05 @ 47.42 s | 70.0 → 52.9 |
  | s09→s10 @ 101.52 s | 69.0 → 42.2 |

Cuts are hard cuts by construction: the 0.5 s inter-scene gap is *inside* the
outgoing scene's segment (video keeps playing, audio goes silent), so there is no
moment where no segment is on screen.

`freezedetect=n=-60dB` flagged four short intervals inside s02 (17.4–21.8 s).
Inspected: not a real freeze. That clip is a near-uniform black platter with only
a small spinning spindle, and the Ken Burns pan across a 2048×1080 source only has
128 px of travel to spend over 10 s (≈12.8 px/s). At −60 dB that reads as static.
Noted as a cosmetic follow-up, not a defect.

### Caption sync cross-check

60 `Dialogue:` lines in `captions/wide.ass`, checked programmatically against the
per-scene windows derived from `project.json` (`start = Σ(duration + 0.5)`):

| scene | window | captions |
| --- | --- | --- |
| s01 | 0.000 – 12.032 | 7 |
| s02 | 12.532 – 22.004 | 6 |
| s03 | 22.504 – 32.211 | 6 |
| s04 | 32.711 – 46.919 | 7 |
| s05 | 47.419 – 56.784 | 5 |
| s06 | 57.284 – 67.161 | 6 |
| s07 | 67.661 – 78.008 | 6 |
| s08 | 78.508 – 90.284 | 6 |
| s09 | 90.784 – 101.024 | 5 |
| s10 | 101.524 – 115.497 | 6 |

- **0** captions straddle a scene boundary — every line falls inside exactly one
  scene's window. The per-scene grouping added late in M1 holds.
- **0** overlapping captions.
- First caption starts at 0.00 s, last ends at 115.18 s against a 115.50 s
  timeline.

### Narration

The script is coherent original prose, on topic, and technically correct. It runs
as a single argument — hook, contrast, mechanism, trade-off, close — rather than
ten disconnected facts, and no sentence repeats another. Full text:

1. When you hit save, the file appears almost instantly, as if the computer whispered the data into existence. The screen lights up, and the operating system reports completion in a fraction of a second.
2. A solid‑state drive can read a megabyte in under a millisecond, while a traditional hard disk needs several milliseconds, a delay you can feel as a brief pause.
3. Hard disks store data on rotating platters, so the read head must wait for the right sector to spin beneath it, creating latency that slows everyday tasks.
4. Before flash memory, computers relied on RAM disks for speed, but those required constant power and lost everything when the machine shut down. They were useful for temporary caches but unsuitable for permanent storage.
5. An SSD is built from arrays of NAND flash cells, each cell trapping electrons to represent a binary 0 or 1 without any moving parts.
6. Data is written in pages of a few kilobytes, but erasing can only happen on larger blocks, so the controller must rewrite whole blocks even for small changes.
7. The SSD controller tracks which cells wear out, spreads writes evenly through wear leveling, and uses error‑correcting code to detect and fix bit errors on the fly.
8. Because each cell can only endure a finite number of program‑erase cycles, manufacturers add extra spare cells and limit write amplification, trading raw capacity for longevity.
9. The result for users is near‑instant boot and silent operation, yet sudden power loss can corrupt unwritten data unless the drive includes a capacitor backup.
10. Solid‑state drives replace spinning platters with electrical storage, delivering speed and durability while accepting limited write endurance as the price of that performance. This balance has reshaped modern computing hardware.

Note the model emits U+2011 non-breaking hyphens ("solid‑state", "error‑correcting",
"program‑erase"). libass renders them correctly and Kokoro pronounces them
correctly, so nothing needs fixing — but any future text processing that splits on
ASCII `-` will miss them.

### Audio

Integrated loudness **−14.9 LUFS** against the `loudnorm` target of `I=-14`, LRA
2.8 LU, true peak −4.3 dBFS (target `TP=-1.5`). Single-pass `loudnorm` lands
within 0.9 LU, which is fine for M1; a two-pass measure-then-apply would hit the
target exactly.

Per-scene narration WAVs are `pcm_s16le` @ 24 kHz mono, written with an explicit
`subtype="PCM_16"` — M0 finding 4 honoured (the downcast is now stated, not
silently inherited). Resampling to 48 kHz happens at mix time, as planned.

---

## Follow-ups for M2/M3

### 1. RESOLVED — Defect: one scene's opening captions were unsynced (whisper returned no timings)

**This is the only real defect the DoD battery surfaced.** In scene 3, the first
seven words came back from faster-whisper with `start_s == end_s == 0.0`:

```
Hard 0.000 0.000   disks 0.000 0.000   store 0.000 0.000   data 0.000 0.000
on   0.000 0.000   rotating 0.000 0.000   platters, 0.000 0.000
so   2.080 2.700   ...
```

Two consequences in the rendered file:

- `captions/wide.ass` contains one **zero-duration** line —
  `Dialogue: 0,0:00:22.50,0:00:22.50,...,Hard disks store data on` — which never
  displays. Those five words are captioned nowhere.
- The next chunk, "rotating platters, so the read", is on screen from 22.50 s to
  25.94 s, i.e. from the very start of the scene, while the narrator is still
  saying "Hard disks store data on". Roughly **2 s of scene 3 shows text that runs
  ahead of the audio**. Confirmed visually at t = 23.0 s.

Scope: 1 scene of 10, 7 words of 284. The other nine scenes have zero
degenerate timings. Root cause is upstream — `faster-whisper base`/int8 dropped
the opening phrase, and snap-to-script assigned the unmatched words 0.0 rather
than interpolating.

**Fixed** (see the two commits following this document's original write-up), in
two layers:

1. `align.snap_to_script`'s `delete` branch no longer collapses an unheard run
   onto the running cursor. The run is now interpolated across the gap it sits in
   — from the previous emitted end to the next *heard* word's start — split
   proportionally to word length by the same `_spread` helper the `replace` branch
   uses. A leading run therefore fills `[0.0, first heard start)` instead of
   piling onto 0.0. A trailing run has no later anchor, so it falls back to the
   scene's measured `duration_s` (passed in by the align stage as the new
   `audio_duration_s` keyword) and stays zero-width only when even that is
   unknown. The one-slot-per-token, in-order, monotonic guarantees are unchanged.
2. `media/ass.py` grew `MIN_DISPLAY_DURATION_S = 0.150` and
   `merge_degenerate_chunks`. Any chunk still shorter than the floor after the
   existing inset/`MIN_CHUNK_DURATION_S` clamp is **merged into the chunk that
   follows it** rather than dropped — the words keep their own (earlier) start and
   stay on screen for the following chunk's window, so nothing is captioned
   nowhere. Merging happens inside `chunk_words`, which `chunk_grouped` calls once
   per scene, so a merge can never pull words across a scene cut. `write_ass`
   additionally skips any chunk below the floor as a last line of defence; with
   the merge in place that guard is unreachable, which is the point.

Regression tests: `tests/unit/test_snap_to_script.py` covers leading, mid-utterance
and trailing unmatched runs; `tests/unit/test_ass_writer.py` asserts no `Dialogue:`
line written by `write_ass` ever has `end <= start`, driven by a run of words all
timed at 0.0, flat and grouped.

### 2. `QuotaTracker` uses a sliding 24 h window; the real APIs reset on a calendar day

Confirmed still accurate: `providers/ratelimit.py` defines `DAY_WINDOW_S = 86400.0`
and `_used()` counts events newer than `now - window_s`. This bites the two
`per_day` budgets — Gemini (240/day) and Cloudflare (9 000 neurons/day) — because
both providers reset on a calendar boundary instead. A budget spent at 21:00
therefore stays spent until 21:00 the next day rather than freeing up at midnight.
(Groq's tracked budget is `rpm` and Pexels' is `per_hour`, and a sliding window
models both of those correctly; only the daily ones are mismatched.)
The error is in the safe direction (we under-spend, never over-spend), and it never
bit this run, but it will confuse anyone who checks the dashboard, sees headroom,
and gets refused locally. Worth revisiting once M2's UI starts *showing* headroom.

### 3. `runner.derive_status` maintains a second, narrower fingerprint

Confirmed still accurate, and now quantified: `cache/stages.json` holds 78 entries
for this project, 34 of them under the `status:` prefix — 44 % of the file exists
only so the status view can answer without a run. `derive_status(project,
stage_cache)` receives neither `Settings` nor the project folder, so it cannot
reproduce the stage hashes (which cover the leading provider *name* and the *bytes*
of files on disk) and compares project-visible inputs instead.

M2's web UI calls `derive_status` constantly — on every poll, for every project in
the list — so this doubles the cache file it reads and keeps two definitions of
"stale" in step by hand. M2 may want to simplify: either pass `Settings` and the
project root into `derive_status` so there is one fingerprint, or move the status
fingerprints into their own file so the two never race on a write.

### 4. `templates/` is not packaged into a wheel — deferred to M6

Confirmed still accurate: `pyproject.toml` has
`[tool.hatch.build.targets.wheel] packages = ["src/videomaker"]`, while
`templates.py` resolves `DEFAULT_TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates"`.
That works for the editable dev install and for CI, and it worked for this DoD run,
but a built wheel would ship no templates. The fix (move templates under the
package, read them via `importlib.resources`) is a packaging decision M6 owns.

### 5. `Uploader.upload`'s signature is provisional pending M5

Confirmed still accurate. `providers/base.py` marks it `PROVISIONAL SIGNATURE —
nothing in M1 calls this`; spec 4.6 wants
`upload(*, video_path, metadata: VideoMetadata, privacy="private", thumbnail_path, progress) -> UploadResult`
and neither `VideoMetadata` nor `UploadResult` exists before M5. M5 owns settling it.

### 6. Pydantic's `model_json_schema()` emits `$ref`/`$defs`; the script stage hand-inlines instead

Confirmed still accurate. `DraftScript` / `DraftScene` exist as pydantic models for
*parsing* the reply, but the schema sent to the provider comes from
`scene_schema(scene_count)`, which is a hand-written fully-inlined dict. Several
structured-output endpoints reject `$ref`/`$defs`, and the mock LLM cannot resolve
them. The inlined version also does something the pydantic one cannot: it pins
`minItems == maxItems == scene_count`, which is why every run returns exactly the
requested number of scenes. Keep the hand-inlining; if a third model ever needs a
schema, factor out a small inliner rather than reaching for `model_json_schema()`.

### 7. Caption chunks are grouped per scene so a caption never straddles a cut

Confirmed still accurate and now verified end-to-end on real output:
`timeline_word_groups()` keeps each scene's words in their own list, and the
cross-check above found 0 of 60 lines crossing a boundary. This was fixed during M1
(commit `fb77b5d`) after a render showed a caption mixing the tail of one scene
with the head of the next. Any M3 work on vertical/karaoke captions must preserve
the grouping — it is not an optimisation, it is the thing that keeps captions
readable at cuts.

### 8. RESOLVED (as a wording fix) — the hardware encoder is detected but never used

`doctor` reports "hardware encoder OK — h264_qsv available for fast renders", and
`media/ffmpeg.py` defines
`HW_ENCODER_PREFERENCE = ("h264_qsv", "h264_vaapi", "h264_videotoolbox")` — but
nothing in the pipeline ever encodes with it. Both `assemble` and `render` hardcode
`libx264`, and the finished file's tag confirms it:
`encoder=Lavc60.31.102 libx264`.

So the constant and the doctor row are currently a promise the pipeline does not
keep. Encoding is 70 s of the 184 s cold run (assemble 32 s + render 39 s); QSV
could plausibly halve that.

**Fixed by softening the claim, not by wiring QSV up** — hardware fast-render mode
is M3 work per the spec, and M3 is the natural home for it since M3 adds a second
aspect and doubles the encode cost. `doctor` now reports
`"<encoder> detected; renders use libx264 (CPU) until fast-render mode lands in M3"`
instead of `"<encoder> available for fast renders"`. The check's `level` semantics
are unchanged (`ok` when an encoder is present, `warn` when none is), and the
"none detected; libx264 (CPU) only" branch is untouched. `HW_ENCODER_PREFERENCE`
stays in `media/ffmpeg.py` for M3 to consume.

### 9. New: stock relevance is keyword-literal

Pexels matched scene 8's query "spare NAND cells, SSD label, capacity sticker" to a
clip of a lab-coated worker beside shelving stencilled "LOAD CAPACITY PER SHELF
200 KG". It matched the *word* "capacity", not the concept. Scene 4's "RAM module,
server rack, power cable" fared better (an actual server rack) but is still generic.

Six of ten scenes are strongly on topic (keyboard, HDD platter ×2, PCB macro ×2, PC
interior); four are generic tech B-roll. Acceptable for M1, and the `candidates`
list is already persisted per scene so a human can re-pick — which is exactly the
affordance M2's storyboard review screen should expose. M3's image fallback would
also help here: a scene whose stock candidates all score poorly is a better
candidate for Flux than a scene with no results at all, which is the only case the
current chain falls through on.

### 10. New: Ken Burns motion is imperceptible on ~16:9 sources

`motion: "pan"` was selected for all ten scenes, but a 2048×1080 source cropped to
1920×1080 only has 128 px of horizontal travel — about 12.8 px/s over a 10 s scene.
It is visible when comparing frames 4 s apart (scene 3 at 23.0 s vs 27.0 s clearly
moved) but reads as static in motion, and it is what tripped `freezedetect` on
scene 2. M3 should either scale the source up before panning so there is real
travel to spend, or pick `motion` based on how much headroom the source actually
has rather than defaulting everything to `pan`.

### 11. New: single-pass `loudnorm` misses its target by ~1 LU

Output measured −14.9 LUFS against `I=-14`. Fine for M1. M3 adds music and ducking,
where a 1 LU error in the narration bed compounds against the music bed — worth
switching to two-pass `loudnorm` (measure, then apply with the measured values)
when that lands.

## What did not need changing

- No source code was modified during this verification. `git status` was clean of
  source edits throughout; the only change is this document.
- `doctor` reported the same 8/8 OK before and after the run.
- The 278-test suite and `ruff check` were green before and after.
