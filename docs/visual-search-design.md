# Visual search quality — design for M3

Agreed as an explicit M3 task. This is the highest-value quality work not
previously scheduled anywhere.

## The measured problem

M1's definition-of-done verification (`spike-results-m1.md`) rated stock
relevance at roughly **6 of 10 scenes strongly on topic**. The clearest failure:
scene 8's query `"capacity sticker"` returned a warehouse shelf stencilled
*"LOAD CAPACITY PER SHELF 200 KG"* — a literal keyword match that is completely
wrong for a scene about NAND cells wearing out.

M2's storyboard gate makes this **manageable** — two clicks to search and swap —
but it does not make it **good**. Curating every scene by hand is the workflow
this tool exists to avoid doing entirely by hand.

## Why it fails

1. **The query is written blind.** The LLM emits `visual_query` at script time
   with no knowledge of what the stock library actually contains. It writes
   phrases that read well and match badly.
2. **Pexels matches keywords, not meaning.** Tags and titles only. Concrete
   single nouns do well; multi-word abstract compounds do badly.
3. **No feedback loop.** M1 takes `candidates[0]` and never asks whether it is
   relevant.
4. **No retry.** A search that returns junk yields junk.

## The work, ranked by value per unit of effort

### 1. Generate several queries per scene, not one (cheapest, biggest win)

The script stage already makes one LLM call. Have it emit **2–3 alternate
queries per scene**, ordered from most specific to most generic, at no extra
call. This alone gives the fallback ladder that item 2 needs.

Tighten `tech_explainer.yaml`'s prompt with **few-shot examples of good vs bad
queries**, not just the current instruction. The instruction already says
"three to six concrete, literal, filmable nouns" and the model still produced
"capacity sticker" — so the instruction is not enough on its own.

### 2. Query laddering with a usable-results floor

Try query 1; if fewer than N candidates survive the existing duration and
resolution filters, try query 2, then a single-noun fallback derived from the
scene's most concrete term. M1's `ResponseCache(ttl_days=7)` already makes
repeats free; the ladder is bounded by the Pexels soft budget of 190/hour, so
cap attempts per scene (3 is plenty).

### 3. Rank candidates instead of taking the first

Free signals already present in the Pexels response: tag overlap with the query
and with the scene's narration keywords, duration headroom over the scene, and
resolution. No model, no quota, no new dependency. Pure function, unit-testable,
and it makes `candidates[0]` mean something.

### 4. Optional visual re-rank with Gemini Flash (the interesting lever)

Metadata ranking cannot see the picture. **Gemini Flash's free tier accepts
images** (1,500 requests/day), so scoring candidate thumbnails against the scene
narration is genuinely available at $0.

Budget honestly: 10 scenes × 4 candidates = 40 images per video, so ~37 videos a
day at the cap. That is fine for this use but not free of consequence, so gate it
— only re-rank when the metadata score is **low or ambiguous**, not on every
scene. Make it configurable and off by default until measured.

Note this needs `AssetRef.preview_url`, which does not exist — the same **models**
change M2's Task 9 already flagged for showing real candidate thumbnails in the
storyboard. Do both at once.

### 5. Denylist the stock clichés

Pexels returns handshakes, generic open-plan offices and stock-smiling people for
a wide range of abstract queries. A small tag denylist removes a recognisable
class of wrong answer for almost no work.

## Measure it, or "improved" is a vibe

**The task is not done without a before/after number.** Build a tiny harness:
run N fixed topics, capture every chosen clip with its scene narration, and score
relevance — either by rating them by hand once (slow, honest) or with an LLM
judge given narration + the clip's tags and alt text (fast, repeatable, and
itself cheap on Groq).

Record the baseline **first**, from the current pipeline, so the improvement is
provable rather than asserted. M1's measured 6/10 is the starting point; state
the target explicitly (8/10 strongly on topic is a reasonable bar).

> **Measured — see `docs/visual-relevance-baseline.md`.** It is a miss: the shipped
> default scores **3.4/10** against the 8/10 bar, and is indistinguishable from the
> M1 path it replaced. M1's 6/10 turned out to be one project of ten, and the fleet
> baseline is 3.6/10. The failure is a homonym problem this plan never touched, and
> item 4's re-rank ships inert. That document, not this one, is the starting point
> for the next attempt.

## What this does not change

The storyboard gate stays exactly as it is. Better search reduces how often you
have to intervene; the gate remains the backstop, and the whole positioning of
the product still rests on a human approving every scene. This work is about
making the default good enough that approving is usually a glance rather than a
repair job.
