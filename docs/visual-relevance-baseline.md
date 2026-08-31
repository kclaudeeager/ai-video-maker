# Visual relevance — the measured before/after

`docs/visual-search-design.md` closes with a binding condition: *"the task is not
done without a before/after number"*. This is that number, measured on 2026-08-31
by `tests/quality/test_visual_relevance.py`.

**It is a miss.** The target was 8 of 10 scenes strongly on topic. The shipped
default measures **3.4 of 10**, and it is not distinguishable from the M1 path it
replaced. The rest of this document is what that means and why.

## The number

Four arms, ten topics, one clip per scene, scored by an LLM judge calibrated
against M1's own hand rating (see [The judge](#the-judge-and-why-it-can-be-believed)).

| arm | what it is | strongly on topic | generic | wrong |
|---|---|---|---|---|
| **before** | the ten finished projects, shots chosen by the M1 code | **36/101 — 3.6/10** | 47 | 18 |
| **before_same_query** | M3 queries, M1 selection (`search()[0]`) | **31/100 — 3.1/10** | 50 | 19 |
| **metadata** *(shipped default)* | Task 12: ladder + cliché drop + ranking | **34/100 — 3.4/10** | 48 | 18 |
| **vision** | Task 12 + Task 13's Gemini re-rank | **34/100 — 3.4/10** | 48 | 18 |

Against a target of **8/10** and M1's recorded **6/10**.

**None of these arms differ from each other.** The 95% confidence interval on 100
scenes at this rate is ±0.9/10, so every arm sits inside every other arm's interval.
The one properly paired comparison — `before_same_query` against `metadata`, same
narration, same first query, only the selection logic changed — moves 21 scenes, 12
up and 9 down: McNemar exact **p = 0.66**. That is a coin.

Task 12 changed the chosen clip on **74 of 100 scenes**. It changed the *quality* of
the choice on none of them.

### M1's 6/10 was one lucky project

The judge scores the ten `how-ssds-work` scenes M1 rated by hand at exactly 6/10,
agreeing scene for scene. The same ruler scores the whole ten-project fleet at
3.6/10. So the M1 spike's headline was not wrong — it was a sample of one, and it
happened to be the best of the ten. **The honest baseline is 3.6/10, not 6/10**, and
the gap to the 8/10 target is more than twice what the plan assumed.

## Why it missed: the failure is a homonym problem, and Task 12 did not touch it

Every one of the 18 outright-wrong picks in the shipped arm is the same failure M1
named. The query asks for a piece of software or an abstract computing idea; Pexels
matches the words against a library of photographs of the physical world; the
physical world wins.

| the scene asked for | Pexels returned |
|---|---|
| `NAND flash cell array` | an aerial view of a **solar panel array** |
| `SSD power regulator module` | a **cell tower** in a field |
| `SSD benchmark speed test` | a **car on a dynamometer** |
| `journal log file view` | a woman cutting a **tree log** |
| `uniform bullet point slide` | children on a playground **slide** |
| `hand pressing control c keyboard` | a hand on an **elevator control panel** |
| `conditional formatting colors` | a time lapse of **clouds** at dusk |
| `save dialog window` | a wedding **"saving the date"** clip |
| `flash memory cells` | **bacteria** under a microscope |

M1's `capacity sticker` → *"LOAD CAPACITY PER SHELF 200 KG"* is the same sentence
written once. Task 12 addressed the query with few-shot examples and the answer with
a ladder, a ranker and a denylist. Neither end touches the mechanism: a specific,
concrete, filmable-sounding noun phrase is *exactly* the input that gets a confident
literal match on the wrong sense, and the ranker then scores that match highly
because the tags really do overlap the query.

The second, larger class is the 48 "generic" picks, and it is a supply problem, not
a ranking one. `pdf viewer on tablet`, `snippet editor window`, `MacBook file save
dialog`, `database import screen` — Pexels does not have footage of software UI, so
the best available answer really is a person at a laptop. No amount of re-ranking a
list of wrong answers produces a right one.

Read together: **the ranker cannot fix a bad candidate set, and the prompt made the
candidate set no better.** If anything the few-shot examples pushed the model toward
tighter compound nouns, which are the phrases most likely to have a physical homonym
— `before_same_query` (M3 queries) scores *below* `before` (M1 queries), though not
significantly.

## Task 13's vision re-rank is inert, and it is not free

`vision` and `metadata` chose **the same clip on all 100 scenes**. Not because the
model agreed — because it never answered. Two defects, both silently swallowed by
`_rerank`'s deliberately broad `except Exception`:

1. **The model is retired.** `GEMINI_MODEL_PREFERENCE` leads with `gemini-2.5-flash`.
   `ListModels` still advertises it, so `_resolve_model` picks it; `generateContent`
   answers `404 ... no longer available to new users. please update your code to use
   models/gemini-3.6-flash`. Every re-rank request 404s.
2. **The reply is truncated.** With a reachable model (`gemini-3.6-flash`),
   `MAX_SCORE_TOKENS = 256` cuts the answer off mid-array — the observed body was
   literally `{"scores": [0.65` — and `_parse_scores` correctly rejects it.

The cost is real. `is_ambiguous` opened the gate on **79 of 100 scenes** — the gate
is far looser than the design doc's "only when metadata is ambiguous" implies — and
`score_images` books the request in a `finally` block, so a 404 costs a unit just
like an answer. Two runs of this arm plus three diagnostic calls spent the entire
**240/day Gemini soft budget**, which is shared with the script stage's LLM fallback.
A user who turned this switch on would lose their day's Gemini allowance and get
back byte-identical output.

Patched by hand — reachable model, 2,048 output tokens — the re-rank does work and
does differentiate: one probe scored four circuit-board candidates
`[0.55, 0.4, 0.3, 0.7]`, which would promote the fourth to first.
`test_vision_rerank_repaired_probe` in the harness measures whether that is an
*improvement*; it could not run on 2026-08-31 because the broken arm had already
spent the budget it needed. **Until it runs, the switch must stay off, and the
reason to keep it off is now stronger than "unmeasured": as shipped it is a
no-op with a bill.**

## The judge, and why it can be believed

The design doc offered two scoring options; this is the LLM judge, on Groq
(`qwen/qwen3.8-27b`, pinned, `temperature=0`, ten pairs per request, cached). Hand
rating ~300 distinct pairs once is not re-runnable, and a number nobody can
reproduce next month is not a baseline.

The judge sees the scene's narration and the clip's Pexels slug — the library's own
title, `a-person-typing-on-the-keyboard` — and nothing else. The slug is the only
description **every** arm has: `AssetRef` never carried `tags`, so the finished
projects cannot offer more, and feeding the newer arms a richer description would
manufacture part of the improvement.

### Calibration

The first rubric asked whether the clip *illustrated the sentence* and rated M1's
ten hand-rated shots **1 of 10** where the human said 6 — a ruler pinned at zero,
with no resolution to detect anything. Explainer B-roll almost never illustrates the
sentence. What the human was accepting was *a picture of the real thing the video is
about*; what they were rejecting was "generic tech B-roll". The rubric was rewritten
to say that, then checked against the same ten shots:

| rubric | judge's score on the anchor | agreement with the human, scene by scene |
|---|---|---|
| v1 "illustrates the sentence" | 1/10 | 5/10 |
| v2 "the kind of thing" | 4/10 | 8/10 |
| v3 v2 + "same physical world" | 8/10 | 8/10 |
| v4 v3 + a broad "generic tech B-roll" clause | 3/10 | 7/10 |
| **v5 (shipped)** v3 + CGI and server rooms only | **5/10** | **9/10** |

Those five were tuned on `openai/gpt-oss-120b`. On the model the harness actually
pins — `qwen/qwen3.8-27b`, chosen when 120b's daily token allowance ran out — v5
scores the anchor **6/10 and agrees on 10 of 10 scenes**: it reproduces M1's hand
rating exactly, same total, same scenes.

Calibrating on the *baseline* arm is deliberate: a ruler tuned on the after-arms
could flatter them; one tuned to reproduce a hand rating of the *before* picture
cannot.

The model matters, which is why it is pinned rather than left to the provider chain.
The identical rubric on `openai/gpt-oss-20b` agrees only 7/10 with the human.
`JUDGE_AGREEMENT_FLOOR` in the harness fails the run if a future edit drops the
agreement below 8/10.

### Spot-checking the judge against the actual pictures

The calibration above is text against text. The judge was also checked against what
the clips **look like**: 24 picks sampled six per arm, thumbnails and extracted
frames laid out as a numbered contact sheet, rated by eye from the picture and the
narration with the judge's verdicts hidden, then compared.

* **19 of 24** exact agreement on the 0/1/2 scale.
* **20 of 24** agreement on the strong/not-strong split that the headline number uses.
* **All four disagreements are the judge being stricter than the human**, never
  looser. It never called something strongly on topic that the eye called generic or
  wrong, so the arms above are not inflated by the ruler.

The four:

| clip | by eye | judge |
|---|---|---|
| unlocking two phones at once, over cloud sync across devices | 2 | 1 |
| server racks, over a metadata service logging block maps | 2 | 1 |
| people working on laptops in an office, over pre-clipboard retyping | 2 | 1 |
| `opening-a-music-file`, over a scene about inode metadata | 2 | 1 |

The last one is the honest limitation of a text judge: the frame shows a file
listing with sizes beside each entry, which is close to what the narration
describes, and the slug says "opening a music file". **The judge scores the slug,
not the shot.** It will therefore undercount clips the library described badly — in
the same direction for every arm, which is what a comparison needs, but it does mean
the absolute 3.4/10 is a floor rather than a point estimate.

## What this says to do next

Ranked by what the measurement actually supports, not by what is interesting:

1. **Stop generating queries blind.** Every failure above is a query written by a
   model that has never seen the library. The design doc's item 1 was implemented as
   *better instructions*; the measurement says instructions are not the lever. The
   lever is a feedback loop — search, look at what came back, and re-query — or a
   vocabulary the model is restricted to.
2. **Prefer a *sense-checked* query to a specific one.** `NAND flash cell array` is
   more specific than `circuit board` and scores far worse, because specificity is
   what creates the homonym. A short list of known-good concrete fallbacks per
   subject area would beat both.
3. **Accept that software UI has no stock footage.** Scenes about dialogs, menus and
   file listings need a screen recording or a generated image, not a stock search.
   This is the strongest argument yet for M3's image fallback being driven by *low
   candidate quality* rather than by *zero results*, which is what M1's spike already
   suggested.
4. **Fix or delete Task 13.** As shipped it is a no-op that spends a shared daily
   budget. Fixing it is two constants; deciding whether it earns its quota needs
   `test_vision_rerank_repaired_probe` to run on a day with budget.
5. **Do not move the goalpost.** 8/10 remains the bar. 3.4/10 is where the tool is.
   The storyboard gate is doing more work than the design doc assumed, and gate 2
   stays slow until this number moves.

## Cost of this measurement

Free tiers only; no money spent. Against this project's own soft budgets:

| provider | requests | budget | notes |
|---|---|---|---|
| Pexels | 105 | 190/hour | 10 scripts × ~10 scenes; the ladder mostly stopped at rung 1 |
| Gemini | 240 | 240/day | **all of it on the broken re-rank**, none of it useful |
| Groq | 495 | 28/min | scripts, the judge, and three rubric calibration passes |

Groq's *token* allowance was the real wall: an unbatched judge exhausted
`gpt-oss-120b`'s 200,000 tokens/day on one pass, which is why the harness batches ten
pairs to a request. Re-running it now costs nothing at all — every search and every
verdict is in `~/.cache/ai-video-maker/responses`.

## Re-running it

```bash
RELEVANCE_HARNESS=1 uv run pytest tests/quality -q -s
```

Opt-in behind an environment variable as well as marked `slow`, because `slow` alone
does not stop a plain `uv run pytest -q` on a machine that has the keys, and this is
the one test in the repository that spends the user's free tier. Raw per-scene
verdicts land in `workspace/quality/visual-relevance.json`.

The harness asserts two things and reports the rest: that the judge still reproduces
M1's hand rating (or the numbers below it mean nothing), and that the shipped default
has not fallen below the 3.4/10 recorded here. **The second is a regression floor,
not an acceptance threshold.** 3.4/10 is not acceptable. It is where we are.

## Known limitations

* **The voice stage is not run.** `visuals` runs after `voice`, so production has a
  real `Scene.duration_s` filtering short clips. The harness estimates it from the
  word count at 2.461 words/second — measured from all 101 real narration recordings
  of these same projects, not guessed.
* **The download is stubbed.** The harness measures which clip is chosen, not whether
  the bytes arrive.
* **The `before` arm has different narration** from the other three, because it is
  the real historical output rather than a replay. `before_same_query` exists to be
  the properly paired control, and it is the comparison the significance test uses.
* **n = 100.** Detecting a 1-point move at this rate needs roughly 400 scenes. The
  harness is re-runnable and cached, so widening it is a matter of adding topics.
