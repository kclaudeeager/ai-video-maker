# M7 — Library + Multimodal Reader Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A reader opens a work in the library, picks a passage, and takes it in
four ways — the source text, a plain-language brief, a narration track, or the
illustrated cut — where each mode is a prefix of the next and upgrading never
redoes paid work. The Bible (World English Bible and Berean Standard Bible,
both zero-restriction) is the first work; nothing about the design is
Bible-specific.

**Architecture:** One new layer *above* projects (`<workspace>/library/`) owning
immutable source text and derived briefs, and one new provider kind
(`CorpusProvider`) so any public-domain book enters the same way scripture does.
`watch` materialises an ordinary `Project` from a reading unit and runs today's
unchanged pipeline. **`STAGE_ORDER` is not touched** — the brief lives in the
library layer and the audiobook is built outside the stage list, on the
`preview.py` pattern.

**Tech stack:** Everything from M0–M6, plus `usfm-grammar` (MIT) for USFM→USJ
parsing at import time only. No new runtime dependency reaches the render path.

## Global Constraints

- Everything from M0–M6 still holds: **$0 cost**, **Python `>=3.12,<3.13`**,
  **Linux x86_64 primary**, **no PyTorch**, **no MoviePy**, **AGPL-3.0-only**,
  **sign off every commit** (`git commit -s`), working directory is the repo root.
- **The tool ships no text whose licence it cannot state in one sentence.** Same
  rule as `assets/music/`, same reasoning, and here it is also the difference
  between a public-domain corpus and a derivative-work problem. See
  `docs/multimodal-reader-design.md` §5.
- **`verbatim` narration is byte-identical to the source.** Not "close", not
  "lightly cleaned". A test asserts it. See §3 of the design doc for why this is
  the milestone's most important guarantee.
- **Non-figurative visuals are the default.** `visuals.depict_figures` defaults
  `False`. Turning it on is the reader's editorial decision and is never implied
  by a template.
- **Do not prepend to `STAGE_ORDER`.** `cache.py`'s comment explains why
  `thumbnail` was appended last; prepending drops every project on disk to `new`.
- Out of scope for M7: paid corpus APIs, non-Latin scripts, a Kinyarwanda voice
  (needs a new `TTSProvider` — its own milestone), publishing/upload.

### Read these first

- `docs/multimodal-reader-design.md` — the design behind this plan. The mode
  ranking (§1), the fidelity model (§3), the visual risk (§4) and the licence
  table (§5) are all normative here.
- `docs/source-driven-video-design.md` — Idea 1 (bring-your-own-text) and Idea 2
  (the LLM wiki). **Idea 1 is a hard prerequisite for Phase C** and is scheduled
  in M4. Phases A and B do not need it.
- `docs/language-support.md` — which languages were *measured* to work, and why
  the binding constraint is the TTS phonemiser rather than the caption font.
- `docs/visual-search-design.md` — the requirement that any visual-quality claim
  be a measured before/after.

### What M0–M6 give you (verified against the tree — do not re-derive)

- `models.py` — `Project` (`id`, `topic`, `template`, `language`, `voice`,
  `target_minutes`, `folder`, `approvals`, `music`, `scenes`, `outputs`,
  `thumbnail_text`, `thumbnail_path`; **no stored `status`**), `Scene`
  (`narration`, `visual`, `audio_path`, `duration_s`, `words`, `beat`,
  `in_short`, `short_pinned`, `locked`, `error`), `SceneVisual`, `AssetRef`,
  `VisualKind` (`AUTO`/`STOCK_VIDEO`/`STOCK_PHOTO`/`AI_IMAGE` — **no `UPLOAD`
  yet**), `Aspect`, `Status`, `clean_folder`.
- `cache.py` — `STAGE_ORDER` (8 stages, `thumbnail` last), `stage_key`,
  `hash_inputs`, `StageCache`, `ResponseCache`, atomic writes.
- `runner.py` — `STAGE_RUNNERS`, `STATUS_AFTER`, `GATE_BEFORE`, `GATE_REVIEW`,
  `derive_status`, `run_pipeline`, `build_deps`, `provider_override`,
  `PROVIDER_KINDS = ("llm","tts","stt","stock","image")`, `STATUS_PREFIX`.
- `providers/base.py` — `LLMProvider`, `TTSProvider`, `STTProvider`,
  `ImageProvider`, `StockProvider`, `VisionProvider`, `LLMResult`, `TTSResult`.
  **This is where `CorpusProvider` goes.**
- `pipeline/script.py` — `run_script`, `DraftScene`, `_OUTPUT_CONTRACT` (already
  bans named people and brands), inlined JSON schema, one repair retry then
  advance the chain.
- `preview.py` — `build_preview` / `build_previews`, keyed `preview:<aspect>` in
  the *same* `StageCache` as the stages but **deliberately outside
  `STAGE_ORDER`**. This is the pattern Task 9 copies.
- `templates.py` + `templates/_schema.md` — `load_template`, `list_templates`,
  `Template.script_fingerprint()` (**`model_dump_json` minus two fields — one
  edited byte stales `script:all` for every project on disk**), unknown fields
  rejected.
- `languages.py` — `LANGUAGES: dict[str, Language]`, `OFFERED_CODES`,
  `language_for(code)`; `Language` is
  `code`/`name`/`espeak`/`script_sample`/`offered`/`note`; offered: en, es, fr,
  it, pt. Imports nothing from the rest of the package, and must keep not doing so.
- `project.py` — `ProjectStore`, `slugify`, `SUBDIRS`, per-project `fcntl` lock.
- `config.py` — `Settings` (pydantic-settings, `.env`), `provider_chains`.
- `web/` — `create_app` factory, 30 routes across `projects`/`script`/
  `storyboard`/`render`, `JobQueue` worker thread, `PasswordGate`, Jinja2
  templates in `web/templates/`, vendored htmx.

### Plan conventions

Same as M1–M3: **Interfaces blocks are normative**, **test code given in full
where it defines behaviour**, implementation prose except where subtle. Every
task ends `uv run pytest -q && uv run ruff check .` green. Mutation-test any
guarantee a test claims to hold.

---

## Phases

| phase | tasks | ends with |
|---|---|---|
| **A — Library** | 1–5 | WEB and BSB imported, addressable, licence-checked. No reader yet. |
| **B — Read and listen** | 6–10 | **The MVP.** Source, brief and narration in the browser. Zero image spend. |
| **C — Scripture on screen** | 11–16 | Verbatim scripts, non-figurative visuals, and a *measured* cost per chapter. |
| **D — Wiki and series** | 17–20 | The LLM wiki, `series.yaml`, gate 0 over an episode plan. |

**Phase B is the milestone's real deliverable.** It is buildable without M4,
costs essentially nothing to run, and answers the question the whole design rests
on — is a brief actually useful? Phase C needs M4's `source_text` entry path.
Phase D is severable and may be dropped without stranding anything above it.

---

# Phase A — Library

### Task 1: `CorpusProvider`, `WorkRef`, `UnitRef`, versification

**Files:** modify `providers/base.py`, `models.py`, `runner.py`
(`PROVIDER_KINDS`), `config.py` (`provider_chains`); test
`tests/unit/test_corpus_contract.py`

**Interfaces:**

```python
class Versification(StrEnum):
    KJV = "kjv"; VULG = "vulg"; LXX = "lxx"; ORG = "org"

class WorkRef(BaseModel):
    id: str                 # "web", "bsb", "rv1909"
    title: str
    language: str           # ISO 639-1, must be a key of languages.LANGUAGES
    licence: str            # one sentence, non-empty
    licence_url: str
    source_url: str
    versification: Versification = Versification.KJV

class UnitRef(BaseModel):
    work_id: str
    book: str               # USFM/Paratext 3-letter code, uppercase: GEN, JHN, REV
    chapter: int            # 1-based
    verses: tuple[int, int] | None = None   # None = whole chapter

    def key(self) -> str: ...   # "web/JHN/003" or "web/JHN/003.16-18"

class UnitText(BaseModel):
    ref: UnitRef
    title: str              # "John 3"
    verses: list[Verse]     # Verse(number: int, text: str)
    word_count: int

class CorpusProvider(ABC):
    @abstractmethod
    def works(self) -> list[WorkRef]: ...
    @abstractmethod
    def outline(self, work_id: str) -> list[UnitRef]: ...
    @abstractmethod
    def unit(self, ref: UnitRef) -> UnitText: ...
```

- `PROVIDER_KINDS` gains `"corpus"`; `provider_chains` gains
  `"corpus": ["bible"]`. Both are additive — `--providers mock` must keep
  overriding every kind, including this one.
- **`WorkRef.licence` is `min_length=1`.** A work with no stateable licence
  cannot be constructed, which is where §5's rule becomes code rather than prose.

**Tests:** a `MockCorpus` in `providers/mock.py` serving three invented chapters;
`UnitRef.key()` round-trips through `UnitRef.parse(key)`; a `WorkRef` with an
empty licence raises `ValidationError`; `provider_override(settings, "mock")`
overrides `corpus` too.

---

### Task 2: Reference parsing

**Files:** new `corpus/refs.py`; test `tests/unit/test_refs.py`

**Interfaces:**
- `parse_reference(text: str, *, work_id: str) -> UnitRef` — accepts
  `"John 3"`, `"Jn 3:16"`, `"John 3:16-18"`, `"JHN 3"`, `"1 Cor 13"`,
  `"1co13"`. Raises `ValueError` naming the input, never guesses.
- `BOOK_NAMES: dict[str, str]` — every common English name and abbreviation to
  its USFM code, including the numbered-book forms (`1 John`, `I John`, `1Jn`).
- `format_reference(ref: UnitRef) -> str` — the human form, `"John 3:16-18"`.

Reference parsing is where a fuzzy-matching instinct causes silent wrongness:
`"Jn"` is John and `"Jon"` is Jonah, and a Levenshtein match will cheerfully
confuse them. **Exact table lookup only, case-folded and space-stripped.** No
fuzzy fallback; an unknown name is an error with the nearest three names listed.

---

### Task 3: The importer, and the licence gate

**Files:** new `corpus/importer.py`, `corpus/usfm.py`; modify `cli.py`; test
`tests/unit/test_importer.py`, `tests/integration/test_import_web.py` (marked
`slow` — it downloads)

**Interfaces:**
- `videomaker library import <work-id> [--from <url|path>]` — writes
  `<workspace>/library/<work-id>/{work.yaml,source/,units/}`.
- `videomaker library list` — table of imported works with licence and unit count.
- `import_work(spec: ImportSpec, root: Path) -> WorkRef`.
- `parse_usfm(text: str) -> list[UnitText]` — via `usfm-grammar` (MIT), USFM→USJ,
  then USJ→`UnitText`. Footnotes, cross-references and section headings are
  **dropped** from `verses[]` and preserved in `source/` — narration must not read
  a footnote marker aloud.

**The licence gate is the point of this task.** ebible.org has no site-wide
licence; each translation's own page is the authority. So:

- `ImportSpec` requires `licence`, `licence_url` and `source_url`. An import
  invoked without them **fails with a message naming the three, and does not
  write a byte.**
- A `--force` flag does **not** exist. There is no hurry that justifies a corpus
  of unknown provenance.
- `NOTICE.md` gains a "Bundled texts" section, and the importer appends to it.

**Tests:** importing a two-book USFM fixture produces the expected `units/`
tree; an import with an empty licence raises before any file is created
(mutation-test this by deleting the check — the test must fail); footnote markers
present in the fixture appear in `source/` and not in any `verses[].text`.

---

### Task 4: WEB and BSB, on disk

**Files:** `corpus/bible.py` (`BibleCorpus`), `corpus/catalogue.py`; test
`tests/unit/test_bible_corpus.py`

`BibleCorpus` implements `CorpusProvider` over `<workspace>/library/`. It holds
no scripture in Python source — it reads what Task 3 imported.

`catalogue.py` carries the *pointers* (id, title, language, licence, licence_url,
source_url) for the texts M7 blesses: `web`, `bsb`, and — commented out with the
reason, not deleted — `rv1909`, `lsg`, `kjv`. Each row is one line the reader of
this file can check against §5 of the design doc.

**Do not commit the texts themselves to git.** ~5 MB per translation, replaceable
by one command, and `.gitignore` already excludes `workspace/`.

---

### Task 5: `videomaker doctor` knows about the library

**Files:** modify `doctor.py`; test `tests/unit/test_doctor.py`

One check: which works are imported, whether each has a stateable licence, and
whether its language is `offered` in `languages.py`. A work whose language has no
measured voice is a **WARN naming the modes it still supports** (`source`,
`brief`), not a FAIL — §6 of the design doc: a language with a text but no voice
can be read, just not heard.

---

# Phase B — Read and listen

### Task 6: Reading units and the library dashboard

**Files:** new `web/routes/library.py`, `web/templates/library.html`,
`work.html`, `_outline.html`; modify `web/app.py`; test
`tests/unit/test_web_library.py`

**Interfaces:**
- `GET /library` — works, with licence shown.
- `GET /library/{work_id}` — the outline (books → chapters).
- `GET /read/{work_id}/{book}/{chapter}` — the reading page.
- `GET /read/{work_id}/{book}/{chapter}/mode/{mode}` — htmx fragment per mode.

`ReadMode` is `StrEnum`: `SOURCE`, `BRIEF`, `LISTEN`, `WATCH`. The page renders
the mode toggle from **what this work's language actually supports** (Task 5's
logic, shared), so an unsupported mode is absent rather than present-and-broken.

Follow `docs/ui-design.md` — achromatic chrome, one warm accent, and the same
folder-tree idiom the project dashboard uses for books.

---

### Task 7: The brief

**Files:** new `corpus/digest.py`; modify `web/routes/library.py`; test
`tests/unit/test_digest.py`

**Interfaces:**
- `build_brief(unit: UnitText, deps: DigestDeps) -> Brief` — one LLM call.
- `Brief(BaseModel)`: `summary: str` (≤120 words, plain language),
  `people: list[str]`, `places: list[str]`, `turn: str` (what changes in this
  passage), `source_ref: str`.
- Cached at `<work>/derived/brief/<unit-key>.<hash>.json`, where the hash covers
  the unit text, the model name and the prompt version — the same three inputs
  every stage fingerprint in this codebase covers, for the same reason.

**The output must be labelled a retelling wherever it is shown**, with the source
mode one tap away. §3 of the design doc: the label is what makes the retelling
worth having, not a disclaimer bolted on afterwards.

Bulk precompute is a CLI command (`videomaker library brief <work-id>`), not a
web route: 1,189 calls fit Gemini Flash's daily allowance and do not fit a
request/response cycle.

---

### Task 8: A reading unit becomes a project

**Files:** new `corpus/materialise.py`; modify `models.py`; test
`tests/unit/test_materialise.py`

**Interfaces:**
- `Project.source: SourceRef | None = None` — new optional field, `None` default,
  so every `project.json` on disk loads unchanged. `SourceRef(work_id, unit_key,
  fidelity)`.
- `materialise(ref: UnitRef, *, mode: ReadMode, store: ProjectStore) -> Project` —
  creates a project whose `topic` is the formatted reference, whose `template` is
  the work's configured one, and whose `folder` is `"<work-id>/<book>"`.
- Re-materialising the same unit **returns the existing project** rather than
  creating a second one. The unit key is the identity.

`Project.source` feeds no stage fingerprint, for the reason `folder`'s docstring
already records: staling `script:all` would let the next run replace `scenes`
wholesale, taking every voiced take and approval with it.

---

### Task 9: The audiobook build

**Files:** new `audiobook.py`; modify `web/routes/library.py`; test
`tests/unit/test_audiobook.py`, `tests/integration/test_listen_path.py`

**Interfaces:**
- `build_audiobook(project, deps, *, cache) -> Path` — concatenates the scene
  narrations into one `output/narration.mp3` with a `chapters.txt` sidecar.
- Cached at `audiobook:<lang>`. **Outside `STAGE_ORDER`, on `preview.py`'s
  pattern** — buildable the moment `align` is current, which is what lets `listen`
  stop before the storyboard gate without a fourth gate or a seventh `Status`.
- Also emits `output/narration.vtt` from the existing word timings, so the reading
  page can highlight the current verse while it plays. The timings are already
  there; this is a serialisation, not a new alignment.

**The end of Phase B is the MVP.** A reader can open John 3, read it, read a
brief, and listen to it, having spent one LLM call and some CPU. Stop here and
use it for a week before starting Phase C.

---

### Task 10: Reader preference, and what it is stored in

**Files:** modify `web/routes/library.py`, `web/templates/base.html`; test
`tests/unit/test_reader_prefs.py`

Preferred mode and preferred language per reader, in a signed cookie. **Not in
`Settings`** — a config file is machine-wide and this is per person, and the app
already has multi-reader shape via `PasswordGate`. Default mode is `BRIEF`, which
is the design's claim about what a busy reader wants, made falsifiable: if
readers immediately switch away from it, §8's first bullet has fired.

---

# Phase C — Scripture on screen

> **Prerequisite: M4's `source_text` entry path (Idea 1).** Do not start Phase C
> until a project can be created from supplied prose.

### Task 11: `source_fidelity` on the template

**Files:** modify `templates.py`, `templates/_schema.md`; test
`tests/unit/test_templates.py`

`source_fidelity: invent | adapt | verbatim`, **default `invent`** — today's
behaviour, unchanged, for every template on disk.

**Read `Template.script_fingerprint`'s docstring before writing a line of this
task.** It is `model_dump_json` minus `short_beats` and `sfx_profile`, and it
records that *adding the field at all* moves every existing template's
fingerprint — which stales `script:all` for every project on disk, and
`run_script` then replaces `project.scenes` **wholesale**, taking every voiced
take, chosen shot and approval built on them. M3 Task 22 walked into that trap
once; this task is the second chance to walk into it.

Neither of the two obvious moves is right. Excluding `source_fidelity` is wrong —
it genuinely decides who writes the narration. Including it naively is wrong —
it destroys every project on disk.

**So change the fingerprint's shape, in the same task.** Replace
`model_dump_json`-minus-two with an explicit ordered dict of the fields the script
stage is a function of, and **omit any field that still equals its default**. Then
every template written before M7 hashes exactly as it does today (nothing has a
non-default `source_fidelity`), `scripture.yaml` hashes differently because it
genuinely is different, and the next field someone adds is free.

**Tests:** every template currently in `templates/` has the same
`script_fingerprint()` before and after this task — pin the current hashes as
literals in the test, which is the only form of this assertion that cannot drift
with the code under it. Then: a template with `source_fidelity: verbatim` hashes
differently from the same template without it.

---

### Task 12: The verbatim segmenter

**Files:** new `pipeline/segment.py`; modify `pipeline/script.py`; test
`tests/unit/test_segment.py`

**Interfaces:**
- `segment_verbatim(unit: UnitText, *, words_per_scene: int, bounds: tuple[int,int]) -> list[SceneDraft]`
  — pure, deterministic, no LLM. Splits on verse boundaries first, sentence
  boundaries within a long verse, never mid-sentence.
- Each draft carries `source_ref` (`"JHN 3:16-18"`).
- `run_script` under `verbatim`: narration comes from the segmenter, and the LLM
  is asked **only** for `visual_queries` per scene, against the existing inlined
  schema minus `narration`.

**The test that matters:**
`"".join(scene.narration for scene in scenes)` equals the source text with
whitespace normalised, for all 66 books of the imported WEB. Mutation-test it by
having the segmenter drop one word — the test must fail.

`Scene.source_ref: str = ""` is added in this task (empty default; old projects
load unchanged) and shown at gate 1.

---

### Task 13: The scripture template and the visual policy

**Files:** new `templates/scripture.yaml`; modify `config.py`,
`pipeline/visuals.py`; test `tests/unit/test_visual_policy.py`

- `Settings.visuals_depict_figures: bool = False`, documented in
  `config.example.yaml` in the house style — what it costs, why it is off.
- `scripture.yaml`: `source_fidelity: verbatim`,
  `visual_kind_order: [stock_photo, ai_image]`, a `structure` of narrative beats,
  and a system prompt whose query contract carries **counter-examples**, in
  `script.py`'s idiom — the module's own comment records that an instruction the
  model can satisfy while being wrong needs a counter-example, not a firmer
  adjective. Include the failure this policy exists to prevent: a query naming a
  holy figure, answered with an invented face.
- With `depict_figures` off, the image provider receives a negative prompt naming
  people, faces and portraits, and the query contract asks for place, artefact,
  architecture, landscape and natural phenomenon.

---

### Task 14: Measure what a chapter costs

**Files:** new `docs/scripture-visual-cost.md`; test
`tests/quality/test_scripture_visuals.py`

**This task builds nothing.** It renders one chapter under Task 13's template and
reports, in the style of `docs/language-support.md` — a record of what happened,
not a capability table:

- neurons spent per finished minute, against the free daily allowance;
- how many scenes stock actually covered versus how many reached the generator;
- a relevance score on M1's 6/10 scale, scored the way
  `docs/visual-search-design.md` requires;
- wall-clock and CPU time.

**If a chapter costs a meaningful share of a day's quota, say so in the doc and
in the UI**, and let §1's ranking stand on measured ground rather than assertion.
Tasks 15 and 16 are contingent on this number.

---

### Task 15: Place photography instead of generated landscapes

**Files:** new `providers/stock/openbible.py`; test
`tests/unit/test_openbible_places.py`

openbible.info publishes Bible place geocoding under CC BY. Combined with
public-domain Holy Land photography, place-name scenes get **photographs of the
actual place** — cheaper than generation and more honest than an invented
landscape.

Contingent on Task 14. If generation turns out to be cheap, this is a quality
improvement rather than a cost fix, and can wait.

---

### Task 16: `watch` in the reader

**Files:** modify `web/routes/library.py`, `web/templates/work.html`; test
`tests/integration/test_watch_path.py`

The `WATCH` mode calls `materialise(..., mode=WATCH)` and hands the reader
straight to the existing gate 1. **The three gates are not bypassed and no
fourth is added**: a reader asking to watch a passage is a creator starting a
project, and the curation argument the project was founded on applies to them
too. Where a render already exists, the mode plays it instead.

---

# Phase D — Wiki and series

Follows `docs/source-driven-video-design.md` Idea 2 without amendment. Summarised
here only for sequencing; write the detailed tasks when Phase C's numbers are in.

### Task 17: `wiki/` ingest — `index.md`, `log.md`, `entities/`, `chapters/`, on Gemini Flash.
### Task 18: `wiki lint` — the contradiction and staleness audit the gist calls for.
### Task 19: `series.yaml` and an episode plan.
### Task 20: **Gate 0** over the episode plan, in the shape of the existing three.

---

## Deliberately not in M7

- **A Kinyarwanda voice.** Kokoro has none, and faster-whisper alignment is weak
  enough that captions timed from the narration would be wrong — the exact failure
  `languages.py` exists to prevent. It needs a new `TTSProvider` behind the
  existing interface, and that is its own milestone. `source` and `brief` work in
  any language that has a text, and M7's mode table degrades per language rather
  than blocking.
- **Any copyrighted translation.** Including via API.Bible, whose terms forbid
  redistributing content and require 30-day cache refresh. It may become a live
  `CorpusProvider` later; its output must never land in `source/`.
- **Bulk `watch`.** 1,189 chapters of generated imagery is not a feature that was
  cut; it is one that should not be built.
- **Commentary, cross-references, study notes.** Real value, entirely separate
  scope, and the wiki layer in Phase D is where they would attach.
