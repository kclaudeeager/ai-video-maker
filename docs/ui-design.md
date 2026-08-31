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
Proposed instead:

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
`videomaker`. If the owner prefers a different word, it is one string in
`_nav.html` plus the `<title>` fallback in `base.html`.

## 3. Palette

The organising idea, and the one thing worth defending:

> **Everything the machine does is cool. The only warm colour on the page is the
> one asking for a human.**

Cool ground, cool ink, cool indigo for anything you can click. Amber appears
exactly where the pipeline has stopped and is waiting for you, and nowhere else.
Green appears only where a human has already signed. That makes "is anything
waiting for me?" answerable from across the room, on any of the five pages,
without reading a word — which is most of the "simple to use, easy to understand
the steps" brief.

### Light (default)

| token | hex | role | measured contrast |
|---|---|---|---|
| `--ground` | `#E9ECF0` | page ground | — |
| `--paper` | `#FFFFFF` | cards | — |
| `--sunk` | `#F3F5F8` | insets: thumbs, code, logs | — |
| `--rule` | `#D3D8E0` | hairlines (decorative) | 1.43 on paper |
| `--rule-field` | `#7C8698` | input & control borders | **3.67** on paper, **3.10** on ground |
| `--ink` | `#14161C` | body text | **18.08** on paper, 15.26 on ground |
| `--ink-soft` | `#4C5462` | secondary text | **7.63** on paper, 6.44 on ground |
| `--accent` | `#333C9E` | links, primary, focus, rail | **9.21** on paper, 7.77 on ground |
| `--on-accent` | `#FFFFFF` | text on a filled accent | **9.21** on accent |
| `--ok` | `#1B6B41` | approved, done | **6.51** on paper, 5.49 on ground |
| `--warn` | `#8A5512` | waiting for you | **6.20** on paper, 5.23 on ground |
| `--bad` | `#A32316` | failed, destructive | **7.48** on paper, 6.31 on ground |

### Dark (`prefers-color-scheme: dark`)

| token | hex | role | measured contrast |
|---|---|---|---|
| `--ground` | `#101318` | page ground | — |
| `--paper` | `#191D24` | cards | — |
| `--sunk` | `#14171D` | insets | — |
| `--rule` | `#2E343E` | hairlines | 1.35 on paper |
| `--rule-field` | `#6B7585` | input & control borders | **3.63** on paper, **4.00** on ground |
| `--ink` | `#E7EAEF` | body text | **14.01** on paper, 15.43 on ground |
| `--ink-soft` | `#A0A8B6` | secondary text | **7.06** on paper, 7.77 on ground |
| `--accent` | `#A6AEFF` | links, primary, focus, rail | **8.14** on paper, 8.97 on ground |
| `--on-accent` | `#101318` | text on a filled accent | **8.97** on accent |
| `--ok` | `#6FD39B` | approved, done | **9.23** on paper |
| `--warn` | `#E7B563` | waiting for you | **9.00** on paper |
| `--bad` | `#F09287` | failed, destructive | **7.38** on paper |

Every ratio above is **computed**, not eyeballed — WCAG 2.x relative luminance,
`(L1+0.05)/(L2+0.05)`. The worst text pair in either theme is 5.23:1, comfortably
past AA (4.5) and past AAA for large text; `--rule-field` clears the 3:1 that
1.4.11 asks of control boundaries against *both* the card and the page ground.

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
