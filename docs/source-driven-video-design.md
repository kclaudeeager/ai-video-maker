# Source-driven video — bring-your-own-text and the LLM wiki

Two related ideas raised by the owner. They are very different sizes and should
not be treated as one feature.

---

## Idea 1 — Bring your own script (small; slot into M4)

Let a creator supply their own prose — typed, pasted, or uploaded as a text file —
and have the system correct, order and segment it into scenes, instead of
generating a script from a topic.

**Why this is cheap:** the pipeline already has the shape for it.

- The `script` stage already emits `scenes[]` with `narration` + `visual_query`.
- Gate 1 already edits every scene and supports split / merge / reorder / delete.
- Everything downstream is agnostic about where the words came from.

What is missing is only an **entry path**: a paste box and a file upload on the
create form, plus a different system prompt — *"segment and adapt this prose into
scenes"* rather than *"invent a script"*. Same output schema, same pydantic
validation, same repair-retry, same gate.

**Roughly 2-3 tasks.** It is also arguably *more* aligned with the project's
positioning than topic→script, because the words are genuinely the creator's own
— the strongest available answer to "is this templated AI content?"

**Design notes**
- Keep `topic` as the fallback path; add `source_text` as an alternative input.
  Both produce the same `scenes[]`.
- The script stage's hash must cover the source text, so editing the upload
  re-segments while leaving voice/visuals for unchanged scenes intact.
- Long inputs need chunking — a 5,000-word script will not fit one prompt
  alongside the template's instructions. Segment in passes, then reconcile.
- **Do not silently rewrite the creator's words.** Adaptation for narration
  (expanding numerals, splitting unreadable sentences) is expected; invention is
  not. Show a diff at gate 1 so the human sees what changed.

---

## Idea 2 — Book → episodes, via an LLM-maintained wiki

Reference: Karpathy's **LLM Wiki** pattern —
<https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f>

### Why this is better than vector RAG here

The gist's argument is that conventional RAG "rediscovers knowledge from scratch
on every query" and that "nothing is built up." Instead the LLM incrementally
maintains a **persistent, compounding artifact**: a directory of markdown pages
(entities, concepts, summaries, cross-references) with an `index.md` catalogue and
an append-only `log.md`, refreshed by an *ingest* workflow and audited by a
periodic *lint*.

For turning a book into a series, that is the right shape for four reasons:

1. **The hard problem is editorial, not retrieval.** A 300-page book into
   10 × 10-minute episodes is ~95% compression. Retrieval finds passages; it does
   not decide where arcs break or what to cut. A wiki of entities, chronology and
   themes is exactly the raw material that decision needs.
2. **Continuity across episodes falls out for free.** Episode 7 reads the wiki's
   entity pages to stay consistent with episode 2, without re-reading the book.
   This was the strongest argument for a new "series" concept — the wiki largely
   *is* that shared context.
3. **No new infrastructure.** No embeddings, no vector store, no extra
   dependency. Plain markdown files. (Note `sentence-transformers` is unavailable
   anyway — PyTorch is banned by this project's constraints. ONNX embeddings on
   the existing `onnxruntime` would have been the fallback; the wiki needs
   neither.)
4. **It matches this codebase's existing philosophy.** Plain files as source of
   truth, hand-diffable, atomic writes, content hashes so work is never redone,
   human gates over automation. `index.md` + `log.md` is recognisably the same
   idea as `project.json` + `cache/stages.json` + derived status.

### Shape

```
library/<work-slug>/
├── sources/          # immutable: the uploaded book, per-chapter text
├── wiki/
│   ├── index.md      # catalogue
│   ├── log.md        # append-only record of what was ingested when
│   ├── entities/     # people, places, organisations
│   ├── concepts/     # themes, mechanisms, turning points
│   └── chapters/     # per-chapter summaries with cross-references
└── series.yaml       # episode plan: order, arc, which wiki pages each draws on
```

Each episode then becomes an ordinary `Project` whose script stage reads
`series.yaml` plus the wiki pages it references — i.e. **Idea 1's
bring-your-own-text path with a richer source.** That is why Idea 1 is a genuine
prerequisite and not a detour.

### Quota shape (this matters)

Ingest is token-heavy: a 300-page book is roughly 100k words ≈ 130k tokens, and
each chunk touches "10-15 wiki pages" per the gist.

- **Ingest on Gemini Flash** — 1M TPM, 1,500 requests/day free. Bulk work fits.
- **Per-episode scripting on Groq** — fast, and the per-call payload is small.

Groq's 6K TPM would make bulk ingest painful; this split is the difference
between feasible and not. Ingest is a one-off per book, so its cost amortises
across every episode.

### What is still genuinely hard

- **Visual sourcing gets much worse, and this is the biggest risk.** M1 measured
  stock relevance at 6/10 for a *tech explainer*, where Pexels has footage. Pexels
  has nothing for 14th-century history. AI images become the primary source, which
  inverts the "stock first, zero neurons" economics M1 measured (a 2-minute video
  cost 0 Cloudflare neurons). A 10-episode documentary series could be the first
  thing in this project that genuinely costs money. Model it before building it.
- **Episode segmentation remains a judgment call.** The wiki informs it; it does
  not make it. Expect a human gate over the episode plan, exactly like the three
  existing gates — a "gate 0" over `series.yaml`.
- **Copyright is a hard boundary.** A video series derived from a copyrighted book
  is a derivative work. Public domain, openly-licensed, or the creator's own
  writing only. This must be stated in the UI at upload time, not buried in docs —
  it is precisely the failure the three-gate curation workflow exists to prevent.
- **Wiki drift.** The gist's "lint" step exists because accumulated knowledge
  bases develop contradictions and stale claims. That is real maintenance work,
  not a one-off.

### Sequencing

- **Idea 1 → M4.** Small, immediately useful, and the foundation for everything
  below.
- **The wiki layer → its own milestone (M7), post-v1.** It is useful on its own
  before any video is made — an ingested book is a browsable artifact.
- **Series/episodes → after that.** Needs the wiki, needs Idea 1, and needs the
  visual-sourcing question answered first.

Do not let any of this into M3. M3 already carries vertical, audio, visual search
and polish.
