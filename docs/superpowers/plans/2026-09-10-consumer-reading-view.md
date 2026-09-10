# The reading view — one app, two doors

> Normative. One commit per task, `git commit -s`, tick the box in the same
> commit. `/CLAUDE.md` applies unchanged.

**Goal:** the person who *takes a work in* gets a surface with no production
vocabulary on it. No gates, no build buttons, no provider names, no stale badges.
They ask for a chapter and they read it, skim it, or hear it.

**The one decision everything follows from:**

> **A reading server is a different *deployment*, not a different *user*.**
> `videomaker serve --reader` starts an app with the studio routers **not
> registered at all**. It cannot create a project, run a stage, approve a gate or
> start a render, because the code that does those things is not mounted.

That is what makes this safe without inventing accounts, roles or permissions.
`PasswordGate` already covers "a household"; the audience flag covers "and they
may only read". Two doors into one corpus, and the door decides what exists.

## What differs, exactly

| | studio | reader |
|---|---|---|
| root `/` | the workspace | redirects to `/library` |
| `/start/*`, `/projects/*` | mounted | **not mounted** — 404 |
| narration missing | "Build the narration" | asks for it, and says "preparing" |
| narration building | job state, stage names | "preparing this — about a minute" |
| brief | names the model | says it is a retelling, and nothing else |
| Watch mode | offered | **not offered** |
| licence | on the shelf, per row | once, in the page footer |

Everything else — the passage, the three modes, the player, the highlighting,
the bookmark — is the same code and the same templates.

## What must not change

- **The corpus is read-only either way.** Nothing here writes to `library/` that
  the studio does not already write.
- **A consumer may not spend the owner's money.** Where a paid voice is
  configured and the estimate is over budget, the studio shows the number and a
  confirm button. The reader shows neither: it says the narration is not
  available and stops. A stranger cannot confirm a bill on somebody else's
  account, so they are not asked to.
- The three gates keep their meaning. Nothing here approves anything.

---

### Task 1: The audience, and what a reading server mounts

- [x] **Files:** modify `config.py`, `web/app.py`, `cli.py`,
  `web/templates/_nav.html`; test `tests/unit/test_audience.py`

**Interfaces (normative):**

```python
class Audience(StrEnum):
    STUDIO = "studio"; READER = "reader"

# config.py
class Settings(BaseSettings):
    audience: Audience = Audience.STUDIO

# web/app.py
def create_app(settings=None, *, providers=None, dev=False) -> FastAPI: ...
```

- `create_app` mounts `projects`, `script`, `storyboard`, `render` and `start`
  **only** for `STUDIO`. `library` and `media` mount either way.
- In `READER`, `GET /` is a 307 to `/library`.
- `videomaker serve --reader` sets it; `config.yaml` can too, under
  `audience: reader`.
- The masthead in `READER` shows the library and nothing else — no Workspace, no
  Start.

**Tests:** every studio path 404s under `READER` and 200s under `STUDIO`; `/` 
redirects; the nav has no studio links; `providers` and `dev` still work.

---

### Task 2: Reading words, not building words

- [x] **Files:** modify `web/routes/library.py`,
  `web/templates/_reader_mode.html`, new `web/templates/_listen_reader.html`;
  test `tests/unit/test_reader_view.py`

- `LISTEN` with no narration yet **asks for it on sight** in `READER` — one
  request, through the same `JobQueue` — and renders "preparing this" while it
  runs. No button, because a button that only ever has one answer is a question
  nobody needed asking.
- The brief drops the model name. It keeps the retelling label: that is the
  feature, not the chrome.
- `WATCH` is absent from `modes_for` under `READER`.
- **A paid voice over budget is not offered to a consumer**, and not confirmed by
  one either: the panel says the narration is not available here.

**Tests:** opening Listen under `READER` enqueues exactly one job and never two;
the page says "preparing" rather than naming a stage; the brief carries no model
name; `WATCH` is absent; an over-budget paid voice offers no confirm control.

---

## Acceptance

```bash
uv run videomaker serve --reader          # then open /
```

The library, a chapter, three modes, audio that arrives on its own. No route on
the site can start a render.

---

### Task 3: A visitor may not spend the whole day's quota

- [x] **Files:** modify `corpus/digest.py`, `config.py`, `web/routes/library.py`,
  `doctor.py`; test `tests/unit/test_reader_budget.py`

**The exposure.** A brief is written on `GET` of the brief mode. On a reading
server that is one LLM call per chapter, triggered by anybody who can reach the
page — and a crawler walking 1,189 chapters of a Bible is 1,189 calls. The
provider budget in `providers/ratelimit.SOFT_BUDGETS` already stops that becoming
a *bill*: Gemini's free tier is capped at 240 a day and the chain refuses past it.
What it does not stop is one visitor spending the owner's whole day by lunchtime.

So the reader gets a budget of its own, **below** the provider's, counted in the
same ledger.

**Interfaces (normative):**

```python
# corpus/digest.py
READER_BRIEF_KEY = "reader:brief"

def reader_budget(settings) -> Budget: ...
def build_brief_within_budget(unit, deps) -> Brief: ...   # raises QuotaExceeded

# config.py
class Settings(BaseSettings):
    reader_briefs_per_day: int = 50
    reader_briefs_per_minute: int = 5
```

Rules, all normative:

- **A cached brief is free and is never counted.** That is the whole point of the
  cache, and it means a popular chapter costs one call ever rather than one per
  reader. The count is of *new* briefs only.
- **The budget applies to `Audience.READER` and to nothing else.** The owner
  working in the studio, and `videomaker library brief`, spend the owner's own
  quota deliberately; they keep the provider budget as their only limit. A
  visitor is the one who should not be able to exhaust it.
- Counted in the existing `QuotaTracker` under `READER_BRIEF_KEY`, so it persists
  across restarts, is shared between the CLI and the web worker, and resets on the
  same UTC boundary as everything else. **No second ledger.**
- Over budget is **not an error page**: the reader is told there is no brief yet
  and the passage is right there, which is the copy that already exists for a
  provider failure. Nothing is billed and nothing is logged as broken.
- `videomaker doctor` reports the headroom, so the owner can see what visitors
  have spent.

**Tests:** a cached brief costs nothing however often it is read; a new one is
counted once; the day's cap refuses the next and the page still reads; the
per-minute cap refuses a burst; the studio is not subject to either; the counter
survives a new `QuotaTracker` over the same file.

---

### Task 4: A reader may watch what exists, and commission nothing

- [x] **Files:** modify `web/routes/library.py`, `web/app.py`,
  `web/templates/_reader_mode.html`; test `tests/unit/test_reader_watch.py`

**Task 2 removed too much.** Two different things share the word *watch*:

* **commissioning** a video — `materialise` creates a `Project`, which needs the
  three gates and a studio to approve them in. Correctly absent for a reader.
* **playing one that already exists** — which is the whole consumer ask, and
  which Task 2 removed along with it.

So on a reading server `WATCH` is offered **only when a render already exists**
for that passage, it plays, and it carries no commissioning control at all. Where
nothing has been made, the mode is absent: there is nothing to watch, and an
empty tab is worse than no tab.

**Interfaces (normative):**

```python
def modes_for(work, settings, *, ref=None, store=None) -> list[ReadMode]: ...
def rendered_for(store, ref) -> tuple[Project, str] | None: ...
```

`modes_for` keeps working with no `ref`: the library shelf asks about a *work*
and cannot know which chapter, so it never offers `WATCH`.

**And a narrow route to serve it.** `media.router` serves any file in any project
directory, which is right for a studio and wrong for a reading server: it would
hand a visitor every script, every take and every `project.json`. So
`GET /media/watch/{work_id}/{book}/{chapter}` resolves the materialised project
for that passage and serves **its rendered wide output and nothing else**, guarded
by the same containment helper as `/media/reading/...`. `media.router` is then not
mounted for `READER` at all.

**Tests:** a reader with no render sees no Watch tab; with one, sees a `<video>`
and no form; the studio still sees the commission button; the narrow route serves
the render and refuses every other path in the project; `/media/{project}/...` is
a 404 on a reading server.

---

### Task 5: Published

- [x] **Files:** modify `corpus/importer.py`, `corpus/bible.py`, `cli.py`,
  `web/routes/library.py`; test `tests/unit/test_published.py`

A work is **published** when its owner says so. Consumers see published works;
the studio sees everything.

**Interfaces (normative):**

```python
class WorkRef(BaseModel):
    published: bool = False        # optional, falsy default

def set_published(work_id, published, root) -> WorkRef: ...
```

- Optional with a falsy default, so every `work.yaml` on disk loads unchanged —
  the same compatibility rule `Project`'s new fields follow.
- `BibleCorpus.works()` is unfiltered; the **reader routes** filter. The corpus
  is a library, not a policy.
- A work that is not published is a 404 on a reading server, not a hidden row:
  the reader must not be able to tell a private work from one that does not
  exist.
- CLI: `videomaker library publish <work-id>` / `--unpublish`, and `library list`
  says which are published.

**Tests:** an existing `work.yaml` with no field loads and is unpublished; the
studio lists both; a reader lists only published; an unpublished work 404s on
every reader route including its chapters and its media; publishing is idempotent.

---

### Task 6: A visitor may not queue the machine solid

- [x] **Files:** modify `corpus/audio.py`, `config.py`, `web/routes/library.py`,
  `web/templates/_reader_mode.html`, `doctor.py`;
  test `tests/unit/test_reader_narration_cap.py`

**The exposure the brief budget does not cover.** A paid voice is guarded by
`TTSBudget` — that one is about money. A *local* voice is CPU: Kokoro runs at
roughly realtime, so a chapter of 51 verses is about six minutes of synthesis. A
visitor walking chapters on a reading server queues that work, and with one
worker the machine grinds. `MAX_QUEUED_JOBS` bounds the queue at 32 and then
raises, which is a wall rather than a policy.

**The unit is minutes of audio, not requests.** Psalm 119 is 176 verses and 2
John is 13; counting both as "one request" would make the cap meaningless at one
end and cruel at the other. `estimate_minutes` already exists — it is what the
paid-voice guard prices with — and `QuotaTracker.record` already takes a unit
count, because Cloudflare's budget is in neurons rather than calls. So this is
the ledger being used as designed.

**Interfaces (normative):**

```python
# corpus/audio.py
READER_NARRATION_KEY = "reader:narration"

def narration_budget(settings) -> Budget: ...
def narration_minutes(unit, *, speed=1.0) -> int: ...

# config.py
reader_narration_minutes_per_day: int = 60
reader_narration_burst_minutes: int = 10
```

Rules, all normative:

- **A reading already on disk is free**, and a request for one is never refused.
  Re-opening a chapter you have heard costs nothing, exactly as re-reading a
  brief does.
- **A request already in flight is free**, and does not count twice: the panel
  polls itself, and a poll must not book minutes.
- **Only `Audience.READER` is capped.** The owner's own machine is theirs to
  grind.
- Both entry points are covered — the auto-request when Listen opens, *and*
  `POST .../audio`, which a reading server still routes even though its template
  offers no button. A guard on the path with a button is not a guard.
- Over the cap is **not an error**: the panel says the narration is not available
  just now and the passage is right there. It reads differently from the
  paid-voice refusal, which will not change by waiting.
- `videomaker doctor` reports the headroom next to the brief one.

**Tests:** a cached reading is free however often it is opened; an in-flight one
is counted once; a long chapter costs more than a short one; the day's cap
refuses the next request and enqueues nothing; the burst cap refuses a rapid
second chapter; the `POST` route is capped as well as the auto-request; the
studio is capped by neither.

---

### Task 7: A narrated work is a podcast

- [x] **Files:** new `corpus/feed.py`; modify `web/routes/library.py`,
  `web/templates/{library,work}.html`; test `tests/unit/test_feed.py`

A work whose chapters have been narrated *is* a podcast, and every player on
earth already speaks RSS. That is the cheapest publishing mechanism available and
it needs no account, no service and no new dependency.

`GET /library/{work_id}/feed.xml`.

**Interfaces (normative):**

```python
MAX_EPISODES = 300

class Episode(BaseModel):
    ref: UnitRef
    title: str
    key: str            # the reading key, which is also the media path segment
    duration_s: float
    bytes: int
    made_at: float

def episodes_for(settings, work_id, *, corpus) -> list[Episode]: ...
def feed_xml(work, episodes, *, base_url: str) -> str: ...
```

Rules, all normative:

- **Only chapters with a finished reading appear.** A feed advertising an episode
  that 404s is worse than a short feed.
- **One episode per chapter**, newest reading wins. A chapter narrated in two
  voices is two files and one episode; a listener did not ask for both.
- **It is a `serial`, numbered in canonical order.** A podcast client sorts newest
  first by default, which for a book is backwards. `itunes:type=serial` plus
  `itunes:episode` is exactly the standard for "start at one and go forward", so
  the ordering is stated rather than fought.
- **The XML is built in Python, not a Jinja template.** RSS enclosures must be
  absolute — a player has no base URL — and `/CLAUDE.md` rule 6 forbids an
  absolute URL in a template, enforced by a test. The rule is about never
  fetching from a CDN and never announcing a page view to a third party; an
  enclosure pointing at this very server is neither. Keeping the generator out of
  the template layer keeps that rule exactly as strict as it was.
- The host comes from the **request**, so a feed works on whatever address the
  listener actually reached — localhost, a LAN address, a tunnel.
- `published` is respected: the feed goes through the same `_work` as every other
  reader route, so an unpublished work has no feed on a reading server.
- Nothing is generated by fetching a feed. It lists what exists, so it needs no
  budget.

**Tests:** the feed parses as XML and validates against the channel/item shape;
only narrated chapters appear; two voices give one episode; episodes are numbered
in canonical order; enclosure URLs are absolute, on the request's host, and
resolve to a real byte-for-byte file; an unpublished work has no feed on a
reading server; a work with nothing narrated yields a valid empty channel.

### Task 8: Take the whole work with you

- [x] **Files:** new `corpus/bundle.py`; modify `web/routes/library.py`,
  `web/templates/work.html`; test `tests/unit/test_bundle.py`

A feed needs a player, a network and a subscription. A zip needs none of those:
it is the offline case, and the one that survives this server being switched off.
It is also the answer to "can I have what I made" that does not involve teaching
anyone about RSS.

`GET /library/{work_id}/bundle.zip`.

**Interfaces (normative):**

```python
MAX_BUNDLE_BYTES = 500 * 1024 * 1024

class BundleTooLarge(Exception): ...

def bundle_name(work: WorkRef) -> str: ...
def bundle_size(settings, work_id, *, corpus) -> int: ...
def write_bundle(path: Path, work: WorkRef, *, settings, corpus) -> None: ...
```

Rules, all normative:

- **The text is always there; the audio is there when it exists.** A work nobody
  has narrated still bundles — that is a book to read on a plane. Gating the whole
  download on narration would make the free-tier deploy, which cannot narrate at
  all, offer nothing.
- **Entries are numbered in canonical order.** `001-GEN-001.mp3` sorts correctly
  in a file manager and in every media player, which sort alphabetically and would
  otherwise put Exodus before Genesis. This is the same problem `itunes:type=serial`
  solves for the feed, solved the same way.
- **The licence travels with the bundle**, as its own file and in the README. A
  redistributable text stops being redistributable the moment it is separated from
  the sentence that says so, and this is the one place the text leaves the tool.
- **mp3s are stored, not deflated.** They are already compressed; deflating them
  spends CPU for under a percent. The text is deflated, where it earns its keep.
- **Over `MAX_BUNDLE_BYTES` is a refusal, not a truncation.** A bundle missing
  half a book, with nothing saying so, is worse than an error naming the size.
- `published` is respected: the route goes through the same `_work` as every
  other reader route.
- Nothing is generated by downloading a bundle. It packages what exists, so it
  needs no budget — the same reasoning as the feed.

**Tests:** the archive opens and `testzip()` passes; the text of every chapter is
present for a work with no narration; a narrated chapter contributes its mp3
byte-for-byte and its VTT; entries sort into canonical order; the licence file
holds the work's licence; mp3 entries are `ZIP_STORED` and text is deflated; a
bundle over the cap raises rather than truncating; an unpublished work is a 404 on
a reading server.
