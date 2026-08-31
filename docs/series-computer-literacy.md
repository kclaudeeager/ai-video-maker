# Series plan — Computer Literacy through MS Office

A series built around Word, Excel and PowerPoint, since that is what "computer
literacy" means in most curricula and to most employers.

**The series is split in two on purpose**, because the two halves need different
footage:

| tier | question | footage | producible |
|---|---|---|---|
| **A — What & why** | *"why does my formatting keep breaking?"* | stock — the pipeline handles it today | **now** |
| **B — How do I** | *"how do I do a mail merge?"* | screen recording of the actual app | needs `VisualKind.UPLOAD` (M4) |

**Tier A is also the less saturated half.** There are ten thousand "how to use
VLOOKUP" videos and very few good explanations of *why a document falls apart
every time you edit it* — which is the actual reason people struggle with Word.
Tier A is what the tool can make now **and** what differentiates the channel.

Every episode is standalone: viewers arrive from search, not from episode 1.

---

## Tier A — produce these now

### A1. Why Your Document Falls Apart Every Time You Edit It
**Hook:** *"If your headings move every time you add a paragraph, you're not formatting — you're decorating."*
**Covers:** styles vs manual formatting; why pressing Enter for spacing breaks; what a heading actually *is* to Word; why this is the difference between a document and a mess.
**Short:** stop pressing Enter to make space.
**Search:** "why does my word document formatting keep changing"

> The most valuable episode in Tier A. It reframes Word from a typewriter into a
> structured document tool, and everything else in Word follows from it.

### A2. A Spreadsheet Is Not Paper
**Hook:** *"Most people use Excel as squared paper. That's why it fights them."*
**Covers:** cells as relationships not boxes; what a formula really is; why typing a number you could have calculated is the mistake underneath most spreadsheet problems.
**Short:** the one habit that makes Excel click.
**Search:** "how does excel actually work", "excel basics explained"

### A3. Never Type the Same Thing Twice
**Hook:** *"Every time you retype something the computer already knows, you're doing its job."*
**Covers:** references, autofill, why copying a formula changes it (relative vs absolute), find-and-replace as a tool rather than a panic button.
**Short:** why your formula broke when you copied it.
**Search:** "excel absolute vs relative reference"

### A4. Save, and Then Find It Again
**Hook:** *"Most people don't lose files by deleting them. They lose them because they never knew where they were saved."*
**Covers:** the folder tree, what a path is, Save vs Save As, why everything lands in Downloads, finding a file you've lost.
**Short:** Save vs Save As — the difference that loses work.
**Search:** "where did my word document save"

> Not an Office feature, but it blocks Office more than any Office feature does.
> Keep it early.

### A5. .docx, .pdf, .csv — and Why It Matters
**Hook:** *"Sending the wrong file type is why your document looked different on their screen."*
**Covers:** what a format is; editable vs fixed; when PDF is right and when it's wrong; why CSV loses your formatting and why that's the point.
**Short:** when to send a PDF and when not to.
**Search:** "difference between docx and pdf", "what is a csv file"

### A6. Why Copy-Paste Breaks Your Formatting
**Hook:** *"You didn't paste text. You pasted text plus everything it was wearing."*
**Covers:** what actually travels with a paste; paste-special and paste-as-plain-text; why pasting from the web wrecks a document.
**Short:** the paste shortcut nobody teaches.
**Search:** "how to paste without formatting"

### A7. Your Slides Are Putting People to Sleep
**Hook:** *"A slide is not a document, and reading it aloud is the fastest way to lose a room."*
**Covers:** one idea per slide; why bullet-dumps fail; slides as support for a speaker rather than a handout; when to use a document instead.
**Short:** the one-idea-per-slide rule.
**Search:** "how to make a good presentation"

### A8. What "The Cloud" Means for Your Documents
**Hook:** *"Your file is in two places at once, and knowing which one you're editing prevents most of the disasters."*
**Covers:** OneDrive/Drive sync, version history as an undo button that survives closing, what collaboration actually does to a file, what happens offline.
**Short:** version history — the undo you didn't know you had.
**Search:** "how does onedrive work", "how to recover a previous version"

---

## Tier B — after `VisualKind.UPLOAD` lands

These are demonstrations. The video *is* the screen, so each needs a screen
recording per scene. Once upload exists, you record once and the pipeline does
narration, captions, timing and both aspect cuts over your footage.

- **B1. Word: styles, headings and a table of contents that updates itself**
- **B2. Word: mail merge, start to finish**
- **B3. Excel: the five functions that cover most real work** — `SUM`, `IF`, `XLOOKUP`/`VLOOKUP`, `COUNTIF`, `TEXT`
- **B4. Excel: pivot tables without the fear**
- **B5. Excel: charts that don't mislead**
- **B6. PowerPoint: slide masters, so every slide matches**
- **B7. The shortcuts that save an hour a week**

Each Tier B episode has a Tier A counterpart to link to — B3 follows A2 and A3
naturally, B1 follows A1. That cross-linking is most of a channel's internal
watch time.

---

## Order

**Produce Tier A in the order listed** — it builds a mental model.

**Publish in search-demand order:** **A1, A4, A6, A2, A5**, then the rest. Every
episode stands alone, so lead with the strongest demand and learn faster. Three
published episodes teach you more than fifteen planned ones.

## Judging whether it works

Views won't tell you, and a new channel's will be low regardless. These separate
*different* problems:

- **Click-through rate** — high impressions, low CTR means the title and
  thumbnail are wrong, not the video.
- **Average view duration** — good CTR with poor duration means the hook or the
  content is wrong.
- **Drop-off shape** — a cliff in the first 15 seconds is a hook problem; a slow
  bleed is pacing.
- **Do the Shorts convert?** Shorts get reach cheaply; the question is whether any
  becomes a subscriber or a long-form view.

Give it **five to eight episodes** before concluding anything.

## Producing them

```bash
cd ~/Documents/Organizations/Personal/ai-video-maker
uv run videomaker serve
```

Or one project per episode:

```bash
uv run videomaker new "why your document falls apart every time you edit it" \
  -t tech_explainer -m 5
uv run videomaker run <id>          # stops at each gate for review
```

**Target 4–6 minutes** for the long cut — roughly 6–9 scenes at the template's
150 wpm. For the Short, untick scenes at gate 2 until the running duration lands
around **35–45 seconds**: keep the hook, one explanatory beat, and the close.

**A `office_explainer` template is worth making** once you have produced two or
three of these — same structure as `tech_explainer` but with a system prompt that
knows the audience is a beginner at a keyboard, and a `visual_kind_order` that
prefers the footage you have uploaded over stock. That is exactly what M4's
template work is for.

## Two things to decide before producing eight of them

**Audience and examples.** These are written neutrally, but they get markedly
better with local specifics — the versions people actually run, the file-sharing
they actually use, the tasks they are actually assessed on. Put that in the
template's system prompt and the model will follow it.

**Language.** English works fully today. Kokoro has no Kinyarwanda voice, and
captions currently render Latin/Cyrillic/Greek only (M3 Task 21). Check any other
language against the supported list *before* producing a series in it.
