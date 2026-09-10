# Unified workspace — one root, three ways in

> Normative, like every plan in this folder. One commit per task, `git commit -s`,
> tick the box in the same commit. `/CLAUDE.md` applies unchanged.

**Goal:** the root of the app stops being "the video project list" and becomes
**the workspace**: one surface that asks what you want to work on, offers the
three ways in, and lists everything already open — a video project and a work in
the library side by side. The reader stops being a second app bolted on.

**The one decision everything follows from:**

> **A reading still does not become a `Project`.** `Project` means the video
> pipeline: `STAGE_ORDER`, three gates, `derive_status`. The unification is a
> *projection* — `web/workspace.py` reads both stores and yields one row type —
> so no reading can stale a stage, trip a gate or move a status. Two storage
> shapes, one surface.

That is what keeps this additive. `cache.STAGE_ORDER`, `Template.script_fingerprint`
and the required fields of `Project`/`Scene` are untouched, and no existing module
changes behaviour.

## The three ways in

They are **alternatives, not a sequence**, and the design must not number them.
What distinguishes them is what each one starts from:

| way in | starts from | becomes |
|---|---|---|
| an idea | a sentence you type | a `Project`: script, gates, a wide cut and a Short |
| a work | a text in the catalogue | a library work: read, brief, listen |
| your own file | `.txt`, `.md`, `.epub` on your disk | the same library work, from your own document |

The third is the one that proves the abstraction generalises past scripture, and
it is why it is in this plan rather than the next one.

## Design direction

`docs/ui-design.md` gains §12 and is the authority; the short version:

- **Charis SIL is the display face.** The app has only ever set its serif at
  16–19px. Setting it at display size is the identity — the product is called
  Longhand and the serif is the longhand.
- **No numbered markers on the three ways in.** They are alternatives.
- **No all-caps eyebrow on the root, no middle-dot meta strings, no `→` in
  button text, no monospace for data labels.** Those are the defaults every
  generated page reaches for; the rest of the app earns its eyebrows by using
  them to say what kind of thing follows, and the root has one kind of thing.
- The palette does not change. Achromatic, one warm hue for what needs a human.

---

### Task 1: `WorkspaceItem` — one list over two stores

- [ ] **Files:** new `src/videomaker/web/workspace.py`; test
  `tests/unit/test_workspace.py`

**Interfaces (normative):**

```python
class Kind(StrEnum):
    VIDEO = "video"; READING = "reading"

class WorkspaceItem(BaseModel):
    kind: Kind
    id: str
    title: str
    href: str
    detail: str          # "waiting at gate 2" | "1189 chapters"
    tone: str = ""       # "" | "warn" | "ok" — the palette's own vocabulary
    needs_you: bool = False
    sort_key: float = 0.0

def workspace_items(settings, store) -> list[WorkspaceItem]: ...
def waiting(items) -> list[WorkspaceItem]: ...
```

A project is `needs_you` when its derived status sits at a gate; a work never is.
Reads only — nothing here writes, and a store that will not load is skipped for
the same reason `project_rows` skips it.

**Tests:** an empty workspace is `[]`; a project and a work appear together,
newest first; a project blocked at a gate is `needs_you` and a work is not; a
broken project directory is skipped rather than raised.

---

### Task 2: The display scale, and §12

- [ ] **Files:** modify `web/static/style.css`, `docs/ui-design.md`; test
  `tests/unit/test_palette_contrast.py` (extend)

New tokens only — no new colours:

```
--t-hero: clamp(2.5rem, 6vw, 4rem);   /* Charis SIL at display size */
--lh-hero: 1.08;
--measure-hero: 22ch;                 /* the headline breaks where it means to */
```

**Tests:** the hero is set in `--font-text` and sized from a token; no page sets
a display size outside the block.

---

### Task 3: The root becomes the workspace

- [ ] **Files:** modify `web/templates/index.html`, `web/routes/projects.py`,
  new `web/templates/_ways_in.html`; test `tests/unit/test_web_dashboard.py`
  (extend), `tests/unit/test_workspace_page.py`

The hero, the three ways in, then what is already open. The create form moves
behind `/start/video` (Task 4), so the root stops being a form with a list under
it.

**Tests:** the root lists a project and a work together; the three ways in are
present and unnumbered; a workspace with nothing in it invites rather than
apologises.

---

### Task 4: `/start` — one place to begin

- [ ] **Files:** new `web/routes/start.py`, `web/templates/start.html`,
  `start_video.html`, `start_read.html`, `start_document.html`; modify
  `web/app.py`; test `tests/unit/test_start_routes.py`

| method | path | is |
|---|---|---|
| GET | `/start` | the three ways in, on their own page |
| GET | `/start/video` | the existing create form, unchanged |
| GET | `/start/read` | the catalogue: what can be imported, with its licence |
| POST | `/start/read` | import one, through the existing gate, on the job queue |
| GET | `/start/document` | the upload form |
| POST | `/start/document` | accept a file, import it, redirect to the work |

`POST /projects` keeps its path and its behaviour: the video form is moved, not
rewritten.

---

### Task 5: A document is a work

- [ ] **Files:** new `corpus/documents.py`; modify `cli.py`; test
  `tests/unit/test_documents.py`

**Interfaces (normative):**

```python
DOCUMENT_SUFFIXES: frozenset[str]     # .txt .md .epub

class DocumentSpec(BaseModel):
    work_id: str
    title: str
    language: str = "en"

def read_document(path: Path) -> list[UnitText]: ...
def import_document(path: Path, spec: DocumentSpec, root: Path) -> WorkRef: ...
```

A chapter is a heading (`#` in Markdown, an EPUB spine item, a blank-line-fenced
`CHAPTER` line in plain text); a verse is a paragraph. The reader then works
unchanged — that is the whole point of the task.

**The licence gate still applies and is still honest.** Your own file is not
redistributed by this project, so the licence recorded is exactly that, and it is
stated rather than blank. There is still no `--force`.

EPUB is a zip of XHTML: `zipfile` plus `html.parser`, both standard library. **No
new dependency.**

CLI: `videomaker library add <path> [--title …] [--id …]`.

**Tests:** each format round-trips to the same chapter/paragraph shape; a file
with no readable text is refused before anything is written; an EPUB's markup
never reaches `verses[].text`; the imported work reads through `BibleCorpus`
unchanged.

---

### Task 6: Uploading one from the browser

- [ ] **Files:** modify `web/routes/start.py`; test `tests/unit/test_upload.py`

`POST /start/document` writes the upload to a temporary path, imports it, and
redirects to the work. A file larger than `MAX_UPLOAD_BYTES` or of an unknown
suffix is refused with the reason on the form, not a 500.

---

### Task 7: The library and the reader in the new direction

- [ ] **Files:** modify `web/templates/{library,work,read}.html`,
  `web/static/style.css`; test `tests/unit/test_web_library.py` (extend)

The library page becomes a shelf rather than a card grid, the work page leads
with the work rather than with a licence line, and the reader keeps everything it
already has. Verified in a real browser, both themes, with screenshots.

---

## Acceptance

```bash
uv run ruff check . && uv run pytest -q
uv run videomaker library add README.md --title "The README"
uv run videomaker serve            # then open /
```

In the browser: the root offers three ways in; `/start/document` takes a file and
lands on a work; that work reads as source, brief and narration like any other.
