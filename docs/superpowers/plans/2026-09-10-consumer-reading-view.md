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
