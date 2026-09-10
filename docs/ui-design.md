# UI design direction — "Longhand"

The direction note for Task 20 of the M3 plan. Written and committed **before**
any CSS, so the reasoning can be argued with cheaply. Everything here is
expressed as tokens at the top of `web/static/style.css`; retuning the palette or
the type scale is a few lines, not a rewrite.

**Judgment call, stated:** one direction, applied all the way, rather than three
half-applied options. Three mockups would each have to be shallow to fit in one
task, and the parts that make a UI feel finished — focus rings, the gate stamp,
the disclosure on gate 2, the dark palette — are exactly the parts that never get
built in a mockup. So: one direction, committed to, with every colour and every
size behind a named token so taste stays the owner's.

---

## 1. What the product actually is

A local-first, single-creator, human-in-the-loop video studio whose thesis is
**curation over automation**. Three human gates; original content; deliberately
not a one-click bot. Everything below is derived from that sentence.

It should read as a **focused craftsman's tool**: calm, confident, a bit
editorial. Explicitly not enterprise SaaS, not a generic admin dashboard, not an
AI-startup neon gradient.

## 2. Name and mark

The nav currently says **AI Video Maker**, which describes the category, not the
product — and the category is exactly what this tool is positioned *against*.

**Decided — the owner approved this name on 2026-08-31:**

> ### Longhand

Automation is shorthand. This tool is the long way round, on purpose: you read
every line, you look at every shot, you sign three times. The name also carries
the milestone: one project yields **the long cut and the Short**. The onboarding
line falls straight out of it —

> **One project, two cuts, three gates.**

**The mark** is the product's own shape: a vertical rule with three crossbars —
one rail, three stops. It is drawn in CSS (three background layers), so there is
no image to ship, it inherits `currentColor`, and it is the same motif as the
stepper rail on the dashboard. The stepper *is* the logo at a larger size.

Only the displayed brand string changes. No package, route, module or config key
is renamed — that is out of scope for a visual pass, and `videomaker` stays
`videomaker`. Should it ever change, it is one string in `_nav.html`
plus the `<title>` fallback in `base.html`.

## 3. Palette

The organising idea, and the one thing worth defending:

> **Everything the machine does is achromatic. The only hue on the page is the
> one asking for a human.**

This started as "the machine is *cool*, the human is warm", with a cool indigo
for anything clickable. It did not survive contact with a real workspace. An
indigo button is still a coloured thing competing for the eye, and on a project
page carrying twelve status pills the amber had nothing to win against — the
page read as a generic admin panel, which is exactly the note the owner gave.

So the rule is taken to its end. **The chrome carries no hue at all**: the
ground, the paper, the ink, the borders, and — the part that is easy to flinch
from — every button and every link. `--accent` is near-black in the light theme
and near-white in the dark one. Links are therefore **underlined rather than
coloured**, which is the more accessible signal anyway.

That leaves exactly one hue in the interface, and it means one thing. Green
survives only on a human's own mark: the approval stamp, and a `done` label.

**The ground is a neutral grey, not a blue-grey.** This app's whole job is
showing you frames to judge, and a cast in the surround is a cast on your
judgement — the same reason a grading suite is painted neutral and a
photographer meters off an 18% grey card. It is also darker than the first pass,
so a white card reads as paper *on* a table rather than a rectangle drawn on a
sheet.

### Three levels of status, not one

The first pass gave every state the same outlined pill. Twelve of them on one
page, all equal weight, told you nothing at a glance. The levels are now:

| level | looks like | means |
|---|---|---|
| default | quiet grey tracked caps, no border | `done`, `pending`, `idle` — the machine's own bookkeeping, readable when you look for it and invisible when you are not |
| `.status-waiting` | warm text | attention or advice: the Short running long, a gate not yet stamped |
| `.status-now` | **the only filled chip in the UI** | this gate is open to *you*, now — `GateRow.reachable`, and nothing else |

Filling every warm state was the first draft of this and it put a shouting chip
on a Short comfortably inside its limit. If a second thing ever earns the fill,
the rule is wrong, not the exception.

**The numbers below are asserted, not asserted-to.**
`tests/unit/test_palette_contrast.py` reads every token back out of `style.css`
and fails the build if any pair drops under AA — including a check that the
chrome tokens really are grey, so the indigo cannot creep back in.

### Light (default)

| token | hex | role | measured contrast |
|---|---|---|---|
| `--ground` | `#DCDBD7` | page ground | — |
| `--paper` | `#FAFAF8` | cards | — |
| `--sunk` | `#EFEEEA` | insets: thumbs, code, logs | — |
| `--rule` | `#C9C7C1` | hairlines (decorative) | **1.62** on paper, 1.22 on ground |
| `--rule-field` | `#6E7276` | input & control borders | **4.64** on paper, 3.50 on ground |
| `--ink` | `#141516` | body text | **17.49** on paper, 13.20 on ground |
| `--ink-soft` | `#585B5E` | secondary text | **6.54** on paper, 4.93 on ground |
| `--accent` | `#22252A` | links, buttons, focus, rail | **14.71** on paper, 11.10 on ground |
| `--on-accent` | `#FAFAF8` | text on a filled accent | **14.71** on the accent |
| `--chrome` | `#1B1D20` | the masthead bar | — |
| `--on-chrome` | `#F5F4F1` | text on the masthead | **15.36** on the bar |
| `--ok` | `#2C6A4B` | approved, done | **6.14** on paper, 4.63 on ground |
| `--warn` | `#9A4408` | waiting for you | **6.27** on paper, 4.73 on ground |
| `--on-warn` | `#FAFAF8` | text on the filled chip | **6.27** on the chip |
| `--bad` | `#A32316` | failed, destructive | **7.15** on paper, 5.40 on ground |

### Dark (`prefers-color-scheme: dark`)

| token | hex | role | measured contrast |
|---|---|---|---|
| `--ground` | `#141517` | page ground | — |
| `--paper` | `#1D1F22` | cards | — |
| `--sunk` | `#17191B` | insets: thumbs, code, logs | — |
| `--rule` | `#2E3033` | hairlines (decorative) | **1.25** on paper, 1.38 on ground |
| `--rule-field` | `#71767B` | input & control borders | **3.60** on paper, 3.98 on ground |
| `--ink` | `#E9E8E4` | body text | **13.47** on paper, 14.90 on ground |
| `--ink-soft` | `#A2A5A8` | secondary text | **6.67** on paper, 7.38 on ground |
| `--accent` | `#EDECE8` | links, buttons, focus, rail | **13.97** on paper, 15.46 on ground |
| `--on-accent` | `#141517` | text on a filled accent | **15.46** on the accent |
| `--chrome` | `#1D1F22` | the masthead bar | — |
| `--on-chrome` | `#E9E8E4` | text on the masthead | **13.47** on the bar |
| `--ok` | `#72C79B` | approved, done | **8.15** on paper, 9.01 on ground |
| `--warn` | `#E9A159` | waiting for you | **7.64** on paper, 8.45 on ground |
| `--on-warn` | `#141517` | text on the filled chip | **8.45** on the chip |
| `--bad` | `#F09287` | failed, destructive | **7.21** on paper, 7.98 on ground |

Every ratio above is **computed**, not eyeballed — WCAG 2.x relative luminance,
`(L1+0.05)/(L2+0.05)` — and it is computed *by the test suite*, from this
stylesheet, rather than typed here and left to rot. See
`tests/unit/test_palette_contrast.py`; regenerate this table from it after any
retune.

`--rule` is decorative and is the one token that does not clear 3:1. It never
carries meaning on its own: every border that has to be *found* rather than
merely seen uses `--rule-field`, which clears 3:1 against both the card and the
page ground as 1.4.11 asks. The worst *text* pair in either theme is 4.63:1,
past AA.

## 4. Type

Two families, each doing one job, plus the system mono that is already there.

- **Space Grotesk** (OFL 1.1) — the chrome. Brand, headings, labels, buttons,
  pills, numbers. A grotesque with real quirks (the flat-sided `o`, the cut `G`,
  the sheared terminals) that reads technical without reading like a dashboard
  template. Weights bundled: Regular 400, Medium 500, Bold 700.
- **Charis SIL** (OFL 1.1) — **the words**. Narration, scene summaries, the
  explanatory prose on every gate page, the onboarding. A Charter descendant:
  sturdy, faceted, made to be read on modest screens.

That split is not decoration, it is the product: **the machine's chrome is set in
the grotesque; anything a human wrote or is about to judge is set in the serif.**
The narration textarea on gate 1 is a serif manuscript, because that is what it
is. Reading a script and reading a status pill are different acts, and the page
should not pretend otherwise.

Bundled under `src/videomaker/assets/fonts/`, served from a new `/assets` mount,
committed unmodified with their OFL text, recorded in `NOTICE.md`. Total ≈930 KB.
No CDN, no build step, no subsetting — subsetting would make them Modified
Versions, and Charis SIL carries a Reserved Font Name.

**Task 15 coordination:** Space Grotesk **Bold** is the face for thumbnail text.
It is wide, high-contrast at small sizes, and already in the repo — Task 15
should read it from `videomaker.assets.FONTS_DIR`, not bundle a second family.

### Scale

A 1.25 major third from a 16px base, with one utility size below it.

| token | rem | px | use |
|---|---|---|---|
| `--t-eyebrow` | 0.6875 | 11 | uppercase section labels, tracked `.14em` |
| `--t-fine` | 0.8125 | 13 | hints, captions, meta |
| `--t-ui` | 0.9375 | 15 | buttons, labels, pills, table-ish text |
| `--t-body` | 1 | 16 | prose and narration |
| `--t-card` | 1.25 | 20 | card titles, `h3` |
| `--t-section` | 1.5625 | 25 | `h2` |
| `--t-page` | 1.9375 | 31 | page title |
| `--t-display` | 2.4375 | 39 | onboarding hero, the render percentage |

Line heights: 1.05 display, 1.2 headings, 1.45 UI, **1.62 prose**. Numbers are
`font-variant-numeric: tabular-nums` everywhere they change in place — the render
percentage, durations, quota counts — so nothing jitters as it counts.

## 5. Spacing and shape

4px base, eight steps: `--s1` 4, `--s2` 8, `--s3` 12, `--s4` 16, `--s5` 24,
`--s6` 32, `--s7` 48, `--s8` 64. Nothing in the stylesheet may invent a gap
outside that ladder.

Radius: `--r-sm` 3px, `--r` 6px, `--r-lg` 10px. Not zero — zero-radius plus
hairline rules is the broadsheet pastiche every generated page reaches for.

Measure: 62rem for the app column, but prose blocks are capped at **34rem** on
their own, because a gate page is a reading task and a 62rem line is not.

## 6. The signature: the rail, and the stamp

**The rail.** The stepper is a real vertical rail down the left of the pipeline —
one hairline, seven stage stops, and three *gate* stops that visibly interrupt it
with a wider bar and a chevron. The gate you are at is the only amber thing on
the page. This is the same figure as the mark, and the same figure as the
onboarding diagram, so the shape of the product is stated three times in three
sizes and never explained twice in words.

**The stamp.** An approved gate is not a green pill; it is a **stamp**: a boxed,
double-ruled, letter-spaced `APPROVED` with the UTC date beneath it, rotated
−1.5°, in `--ok`. Approving is the one thing in this product only a human can do,
and a rubber stamp is what that act looks like. It appears at most three times in
a project's life, which is exactly the budget for a flourish. Everything around
it stays quiet — this is the one accessory, and nothing else gets one.

## 7. Component inventory

| component | where | notes |
|---|---|---|
| Shell + nav | every page | brand + mark, Projects, Guide; sticky, hairline under |
| **Next-step panel** | dashboard, all 3 gate pages | eyebrow (where you are) · one sentence (what you are judging) · one primary button. Derived from `derive_status`/`GATE_BEFORE`/`GATE_REVIEW`. Zero tokens, cannot be wrong |
| Stepper rail | dashboard | 7 stages, 3 gate stops, current stop amber |
| Gate card | dashboard, all 3 gate pages | state, what it asks, the stamp when signed |
| Status pill | everywhere | uppercase, tracked, hairline, `currentColor` |
| Scene card (script) | gate 1 | serif narration textarea, query field, ops row |
| Scene card (storyboard) | gate 2 | shot + 9:16 overlay always visible; **everything that changes it inside a `<details>`** |
| Candidate tile | gate 2 | picture is the button; chosen tile ringed in `--ok` |
| Short meter / quota meter | gate 2 | progress + plain sentence |
| Artefact panel | gate 3 ×2 | video, percentage, path field, download |
| Job panel | dashboard | polled fragment, fades in on settle |
| Notice | everywhere | bordered, not filled — part of the document, not a modal |
| Empty state | everywhere | says what to do next, never just "nothing here" |
| **Onboarding** | `/` when empty, `/guide` after | four sentences, one CSS diagram, live free-tier headroom |

## 8. Progressive disclosure on gate 2

Gate 2 is the long scroll — five scenes today, ten later. The fix is not more
chrome, it is less of it at rest. Each scene card keeps **only the review** in
view: number, narration summary, the chosen shot with its 9:16 window, its state.
Everything that *changes* the shot — motion, framing, in-the-Short, re-voice,
stock search, and the alternatives grid — moves inside one `<details>` per card,
labelled with what it holds and how many alternatives there are.

It opens by default exactly when the scene needs attention (no chosen shot, or a
problem was reported), so the page still lands on the thing that is wrong. Every
form, field name and `data-*` attribute stays in the markup unchanged, and
`<details>` needs no JavaScript, so both the M2 contracts and the no-JS
requirement survive.

## 9. Motion

htmx swaps land with no transition today, so the page twitches. Fix: elements
carry `.htmx-settling` for a beat after insertion; that beat is a 160ms opacity
and 2px lift. Nothing else moves — no page transitions, no scroll effects, no
hover lifts. Under `prefers-reduced-motion: reduce` every duration collapses to
`0.01ms`, including the `<details>` marker rotation.

## 10. Accessibility floor

- Every control has a real `<label>`; nothing relies on a placeholder.
- Focus is a 2px `--accent` outline with a 2px offset, on a `:focus-visible`
  basis, and it is never removed — including on the candidate tiles, whose whole
  picture is the button.
- Contrast is measured (§3), in both themes, for text and for control borders.
- It works with JavaScript off: every control is a real form first, `<details>`
  is native, and the one hand-written script is a live preview, never a save.
- The stamp's rotation and its rules are decorative; its text is the state, so a
  screen reader hears "approved 2026-08-31 14:02 UTC", not "stamp".

## 11. The workspace root is a dashboard, not a list

M3 shipped the project list as an always-expanded `<details>` tree, reasoning
that you came back to *find* a project rather than to click your way to one. At
ten projects that reasoning inverted. Every card was the same height, the same
weight and the same grey pill; the page was 2215 px of identical rows, and
finding anything meant reading all of them. The tree also drew nothing at all,
because no project had ever been filed — a feature with an empty state that looks
exactly like a missing feature.

The root now answers one question — **is there anything for me, and where is it**
— and delegates the rest:

- **A summary strip**, first, in amber: `N of M projects need you`. It is
  *omitted entirely* when nothing is waiting rather than printed as a zero. A
  dashboard that says `0 waiting for you` every day teaches you to stop reading
  it, and then it cannot warn you.
- **Folder cards**, each the whole clickable row: name, count, and an amber
  `N waiting` badge on the far edge so a column of them can be scanned without
  reading a single folder name. The count and the badge both reach *through* the
  children, or a root card could never tell you there was a reason to open it.
- **Projects filed at the root**, under a heading only when there are folders to
  tell them apart from.

A folder is an `<a>` to `/folders/<label>`; that page shows the folders inside it
and the projects filed directly in it, under a breadcrumb whose every level is a
real link. The tree is still built whole per request — it is derived from the
rows and costs nothing — and each page renders one node of it.

**Amber still means exactly one thing.** The `waiting` count is
`WAITING_STATUSES`, which is `runner.GATE_BEFORE`'s three review points seen from
outside: `script_ready`, `storyboard_ready`, `preview_ready`. `new` and `voiced`
are the machine mid-stride and `rendered` is finished; none of the three is
anything a person can act on, so none is counted. A folder page prints its count
even at zero, but greyed (`.is-quiet`) — there the strip is a status line rather
than an alarm.

A folder nothing claims is a **404**, not an empty page. Folders exist only
because projects claim their labels, so inventing an empty node would render a
typo in the address bar as a real, permanently empty folder.

Navigation stays the browser's job: a link and a 303, no JavaScript, per §10.

---

## 12. The workspace root, and the three ways in

§11 said the root is a dashboard rather than a list. This section says what it is
a dashboard *of*, now that the product has more than one kind of thing in it.

**The root is the workspace.** It asks one question — what do you want to work
on — and it answers three ways: from an idea, from a work, from a file of your
own. Underneath, it lists what is already open, and a video project and a text in
the library sit in the same list. They are two storage shapes and one surface;
`web/workspace.py` is the projection that makes that true without giving reading
a state machine it does not need.

### The serif goes large, once

Charis SIL has been in this project since M3 and has never been set above 19px.
That is why the app reads as an admin panel with prose inside it: the only voice
with any character in it was whispering. The root's opening line is set in
`--t-hero` — fluid, 40 to 64px — and it is the **one** place a display size is
allowed. Everything else keeps the scale §4 already defines.

This is also the identity argument. The product is called Longhand because
automation is shorthand and this is the long way round on purpose; the serif is
the longhand. Setting it large is the cheapest true thing the design can say.

### What the root must not do

Four treatments are banned on this page specifically, and the ban is worth
writing down because each of them is what a page like this reaches for by
default:

- **No numbered markers on the three ways in.** `01 / 02 / 03` encodes a
  sequence, and these are alternatives — you pick one. The gate rail on a
  project page *is* a sequence and keeps its numbers.
- **No all-caps eyebrow above the hero.** The eyebrow earns its place elsewhere
  by saying what kind of thing follows; the root has one kind of thing, so an
  eyebrow there would be decoration wearing a label's clothes.
- **No middle-dot meta strings.** `3 projects · 1 work · 2 waiting` says nothing
  the list below it does not say better and in full sentences.
- **No arrow appended to a link or a button.** "Start from an idea" is already a
  verb; the arrow adds a glyph and no information.

### What distinguishes the three, visually

Not a number and not an icon: **what each one starts from**. An idea starts from
a sentence you have not written yet, so its card shows the field. A work starts
from something already published, so its card shows a title and a licence. Your
own file starts from something you already have, so its card shows a filename.
The card is a preview of the input, which is the one honest difference between
them.

### Contrast, unchanged

The palette does not move. Achromatic chrome, one warm hue for the thing asking
for a human, and every value still a token at the top of `style.css` that
`tests/unit/test_palette_contrast.py` reads back out and measures.

