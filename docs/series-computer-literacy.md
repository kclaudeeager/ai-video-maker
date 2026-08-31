# Series plan — Basic Computer Literacy

A 12-episode series you can start producing today with the existing
`tech_explainer` template. Every episode is a **standalone video**: viewers arrive
from search and suggested, almost never from episode 1, so each one opens cold and
assumes nothing.

Each entry gives the **title** (search-shaped, not clever), the **hook** (the first
spoken line — the only line that decides whether they stay), what it covers, and
the **Short angle** (the one idea that stands alone in 35 seconds).

---

## The episodes

### 1. Why Your Computer Has Three Kinds of Memory
**Hook:** *"Your computer stores your work in one place, thinks in another, and confusing the two is why it feels slow."*
**Covers:** CPU, RAM, storage — and specifically why RAM is not storage, the single most common beginner confusion.
**Short:** RAM vs storage, in one analogy.
**Search intent:** "what is RAM", "RAM vs storage difference"

### 2. Where Your Files Actually Go
**Hook:** *"Most people don't lose files by deleting them. They lose them because they never knew where they were saved."*
**Covers:** the folder tree, what a path is, Save vs Save As, why everything ends up in Downloads, finding a file you've lost.
**Short:** Save vs Save As — the difference that loses people's work.
**Search intent:** "where are my downloads", "how to organise files on computer"

> This is the highest-value episode in the series. File location is the thing that
> blocks beginners from every other task.

### 3. What an Operating System Actually Does
**Hook:** *"Windows, macOS and Linux are three answers to the same question — and knowing the question makes all three easier."*
**Covers:** what an OS is for, why updates exist, why software is built "for" a system.
**Short:** why you should install that update you keep postponing.
**Search intent:** "what is an operating system"

### 4. The Internet, the Web, and Your Browser Are Three Different Things
**Hook:** *"Three different things get called 'the internet', and telling them apart is how you fix problems yourself."*
**Covers:** ISP → connection → the web → the browser; why the distinction is practical, not pedantic.
**Short:** the difference in 30 seconds.
**Search intent:** "difference between internet and web"

### 5. How to Actually Search
**Hook:** *"Most people use a search engine at a fraction of what it can do."*
**Covers:** specific over vague, quotes for exact phrases, `site:`, reading results critically, why the top result isn't always the answer.
**Short:** three search tricks.
**Search intent:** "how to search google effectively"

### 6. Your Password Is Probably Already Public
**Hook:** *"The danger isn't someone guessing your password. It's that one you reused was leaked years ago."*
**Covers:** why reuse is the real risk, password managers, two-factor authentication, what actually makes a password strong.
**Short:** why length beats symbols.
**Search intent:** "how to create a strong password", "what is 2FA"

### 7. How to Spot a Scam Before You Click
**Hook:** open on a real-looking message and take it apart.
**Covers:** manufactured urgency, mismatched links, checking the actual sender, the habit of never acting from a message.
**Short:** one message, three warning signs.
**Search intent:** "how to spot a phishing email", "is this message a scam"

> Episodes 6 and 7 are the highest-stakes in the series and among the
> highest-search. See the publishing-order note below.

### 8. Installing Software Without Breaking Your Computer
**Hook:** *"Almost nobody gets a virus from a website. They install one on purpose."*
**Covers:** official sources, bundled extras in installers, what permissions mean, uninstalling properly.
**Short:** the checkbox in installers you should always untick.
**Search intent:** "how to safely download software"

### 9. Backups: Twenty Minutes That Saves Everything
**Hook:** *"Every storage drive fails. The only question is whether it takes your work with it."*
**Covers:** why one copy isn't a copy, cloud vs external drive, what's actually worth backing up, a setup you do once.
**Short:** the one rule — two copies, two places.
**Search intent:** "how to back up my computer"

### 10. Why Your Internet Is Slow
**Hook:** *"'Slow internet' is usually one of four things, and three of them you can fix yourself."*
**Covers:** router placement and signal, bandwidth vs latency, how many devices share a connection, what to check first.
**Short:** the fix that works most often.
**Search intent:** "why is my wifi slow"

### 11. Documents That Don't Fall Apart
**Hook:** *"If your formatting breaks every time you edit, you're formatting by hand."*
**Covers:** styles instead of manual formatting, PDF vs editable, sharing so it looks the same for everyone.
**Short:** stop pressing Enter to make space.
**Search intent:** "how to format a document properly"

### 12. What to Do When It Doesn't Work
**Hook:** *"There's an order to fixing things, and 'restart it' is genuinely first for a reason."*
**Covers:** the troubleshooting ladder, error messages as information rather than noise, how to search an error usefully, when to stop and ask.
**Short:** how to search an error message.
**Search intent:** "how to fix computer problems"

A good closer: it makes every earlier episode more usable and is a natural
"watch next" from any of them.

---

## Order

**Production order is the list above** — it builds a mental model first (1–3),
then connectivity (4–5), safety (6–8), practical habits (9–10), and productivity
plus a closer (11–12).

**Publishing order should probably be different.** Every episode is standalone, so
lead with the strongest search demand — **2, 7, 6, 10, 1** — and hold the rest.
Three published episodes teach you more about whether anyone watches than twelve
planned ones.

## How to actually judge whether it's working

You said the point is to *"see if anybody would watch them."* Views alone won't
tell you, and on a new channel they'll be low regardless. Watch these instead:

- **Impressions click-through rate** — is the title and thumbnail earning the
  click? If impressions are high and CTR is low, the *packaging* is wrong, not the
  video.
- **Average view duration / percentage viewed** — did they stay? If CTR is fine
  but duration is poor, the *content or the hook* is wrong.
- **Where viewers drop off** — YouTube shows this per video. A cliff in the first
  15 seconds is a hook problem; a slow bleed is a pacing problem.
- **Whether the Shorts convert** — Shorts get reach easily; the question is
  whether any of it turns into a subscriber or a long-form view.

Give it **five to eight episodes** before drawing conclusions. One video tells you
nothing; a channel's early videos almost always underperform its later ones.

## Producing them

```bash
cd ~/Documents/Organizations/Personal/ai-video-maker
uv run videomaker serve      # then work through the three gates in the browser
```

Or from the CLI, one project per episode:

```bash
uv run videomaker new "why your computer has three kinds of memory" -t tech_explainer -m 4
uv run videomaker run <id>   # stops at each gate for review
```

**Target 4–6 minutes for the long cut.** Long enough to be substantial, short
enough to hold a beginner. That's roughly 6–9 scenes at the template's 150 wpm.

For the Short, untick scenes at gate 2 until the running duration lands around
**35–45 seconds** — keep the hook, one explanatory beat, and the close. Do not
publish a 2-minute Short; see `docs/superpowers/plans/2026-08-31-m3-dual-format-and-polish.md`
Task 22.

## Two things worth deciding before you start

**Audience and examples.** "Basic computer literacy" plays very differently for a
student in Kigali and a retiree in Kansas. The episodes above are written
neutrally, but they get substantially better when the examples are local — the
services people actually use, the prices they actually pay, the scams actually
circulating. Decide who this is for and put it in the template's system prompt;
the LLM will follow it.

**Language.** Kokoro has no Kinyarwanda voice, and captions currently render only
Latin/Cyrillic/Greek scripts (see Task 21). English works fully today. If the
series should be in another language, check it against the supported list before
producing twelve episodes.
