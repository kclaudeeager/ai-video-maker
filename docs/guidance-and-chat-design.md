# Guidance, onboarding, and a conversational mode

Owner request: *"add the chat that can help user finish everything in a
conversational way if they prefer, especially new users who don't understand how
it works... would also create the onboarding screen"*.

Three separable pieces, in ascending order of cost. Do them in this order.

---

## 1. Deterministic next-step guidance (cheap — do this first)

**Most newcomer confusion is "what do I do now?", and the app already knows.**
`derive_status` returns exactly which stage is pending and which gate is
unapproved; `GATE_REVIEW` already carries a sentence about what each gate asks
the human to judge. Turning that into a prominent, plain-language "next step"
panel costs **zero LLM tokens, zero latency, and cannot be wrong** — it is read
from the same source of truth the pipeline runs on.

Rough shape: one line saying where you are, one line saying what you are being
asked to judge and why, one primary button. On the dashboard and at the top of
each gate page.

This is worth building even if the chat never happens, and it removes most of
what the chat would otherwise be asked.

## 2. Onboarding screen (small — folded into Task 20)

Belongs with the visual design pass, whose brief is already *"simple to use and
easy to understand the steps"*. A first-run screen that explains, in four
sentences and a diagram:

- what the tool makes (a long video **and** a Short, from one project)
- the three gates, and that **nothing publishes without you**
- what it costs (nothing — free tiers; show the current headroom)
- what it needs (API keys; link to `doctor`)

Show it when `list_ids()` is empty; reachable afterwards from the nav. Do not
build a multi-step wizard — a single screen someone reads once.

## 3. Conversational mode (a milestone of its own — after M3)

### What it actually is

Not a chatbot: an **agent with tools**, where the tools are the 24 routes that
already exist. "Make me a video about SSDs" → create project → wait → summarise
the script → ask for changes → approve → and so on.

### The constraint that shapes the whole design

The product's thesis is **human curation at three gates**. A chat that finishes
everything unattended is structurally the one-click bot the spec positions
against, and platforms demonetise. So:

> **The chat is a different door to the same gates, never a bypass around them.**

Every gate still requires an explicit human approval, expressed as a turn
("approve" is a decision the human makes, not one the model infers from
enthusiasm). The chat's job is to make that approval *informed and one sentence
long* instead of a page of scanning. It may summarise, propose, and act — it may
not approve.

### Cost, honestly

Every turn is an LLM call competing with script generation on the same free
tier, and **Groq's 6K TPM is the binding limit**, not requests per day. A chatty
onboarding could plausibly cost more tokens than the video it produces.

Mitigations, in order:
- **Deterministic first.** Piece 1 above answers most questions with no model at
  all. The chat should fall through to it, not duplicate it.
- **Small model for chat, big model for scripts.** `openai/gpt-oss-20b` or
  `groq/compound-mini` is plenty for "what does this gate mean"; reserve
  `gpt-oss-120b` for narration.
- **Reuse the existing `ResponseCache`.** Explanatory answers repeat across users
  and sessions.
- **Show the cost.** The storyboard's free-tier headroom panel already exists;
  the chat should sit under the same budget and say so.

### Architecture notes

- **It fits the existing worker model.** Handlers never do long work (design
  decision 2); a chat turn is enqueued like any other job and the page polls,
  exactly as `_job.html` already does. No new concurrency model, no websockets.
- **Tools are the existing routes**, called in-process — not over HTTP to itself.
  Each tool needs a narrow, typed signature and must respect the same locks and
  no-op guards the routes do.
- **The transcript is per-project state.** It belongs beside `project.json`, not
  in memory: a chat that forgets on restart is worse than no chat.
- **A tool call that mutates must be echoed back in plain language.** "I changed
  scene 3's search to *NAND flash macro* and found four new clips" — otherwise the
  human is approving something they did not see.

### What would make it genuinely good rather than a novelty

Answering questions the UI cannot: *"why does scene 4 look wrong?"* (it can read
the narration and the chosen clip's tags), *"make the script less formal"* (it can
edit every scene and show a diff at gate 1), *"which scenes should I cut from the
Short?"* (it can read durations against the 180 s limit). Those are judgements
across the whole project, which is exactly what a per-scene UI is bad at.

---

## Sequencing

| piece | when | size |
|---|---|---|
| Deterministic next-step guidance | **M3, with Task 20** | small |
| Onboarding screen | **M3, folded into Task 20** | small |
| Conversational mode | **post-M3, own milestone** | large |

Do not let piece 3 into M3. M3 already carries vertical, audio, visual search and
polish.
