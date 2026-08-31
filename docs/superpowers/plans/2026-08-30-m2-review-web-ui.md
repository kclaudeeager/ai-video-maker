# M2 — Review Web UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `videomaker serve` opens a local web UI where a whole project is taken from topic to finished `final_wide.mp4` without touching the CLI — pausing at all three review gates — and editing one scene at the storyboard gate re-generates only that scene.

**Architecture:** FastAPI + Jinja2 server-rendered pages, htmx for partial updates, no npm and no build step. Long work never runs in a request handler: a single daemon worker thread pulls jobs off a `queue.Queue` and calls M1's `run_pipeline`, publishing progress into a `JobState` that the page polls every 1.5 s. Every route is a thin adapter over M1's existing `ProjectStore`, `runner`, and pipeline stages — **M2 adds no pipeline logic.**

**Tech Stack:** Everything from M1, plus `fastapi`, `uvicorn`, `jinja2`, `python-multipart`, and a vendored `htmx.min.js` (~14 KB, committed, no CDN).

## Global Constraints

- Everything from M0 and M1 still holds: **$0 cost**, **Python `>=3.12,<3.13`**, **Linux x86_64 primary**, **no PyTorch**, **no MoviePy**, **AGPL-3.0-only**, **sign off every commit** (`git commit -s`), working directory is the repo root.
- **Scope is the three review gates, wide only.** Vertical/9:16, karaoke captions, music and the music picker, thumbnails, and `clean` are **M3**. Metadata and upload are **M5**. Docker is **M6**. Do not build them; do not design them out.
- **No authentication, and that is deliberate.** The server binds `127.0.0.1` by default. `--host 0.0.0.0` must print a loud warning, because there is no auth and the app can read and write anywhere the user can.
- **No npm, no CDN, no build step.** htmx is committed into the repo. A page must render usefully with JavaScript disabled wherever that is cheap to achieve (forms are real forms; htmx enhances them).
- **The pipeline is unchanged.** If a task finds itself editing `src/videomaker/pipeline/`, stop and reconsider — the only sanctioned pipeline-adjacent addition in M2 is the 480p preview artefact (Task 11), and it is deliberately *not* a `STAGE_ORDER` stage.

### What M1 gives you (verified, do not re-derive)

- `videomaker.project.ProjectStore` — `create`/`load`/`save`/`list_ids`/`path_for`/`scene_dir`/`lock`. `save()` is atomic; `lock()` is a blocking `fcntl.flock` context manager.
- `videomaker.runner` — `build_deps(settings, project_id, *, cache_dir=None) -> StageDeps`, `stage_cache_for(store, project_id)`, `derive_status(project, stage_cache) -> Status`, `run_pipeline(project, deps, *, until=None, yes=False, on_stage=None) -> Project`, `stages_through(until)`, `stage_is_current(project, stage_cache, stage)`, `clear_stale_approvals`, `provider_override(settings, name)`, `GateBlocked`, `StageFailed`, `STAGE_RUNNERS`, `STATUS_AFTER`, `GATE_BEFORE`, `GATE_REVIEW`, `PROVIDER_KINDS`, `USER_CACHE_DIR`.
- `GATE_BEFORE` is `{"voice": "script", "captions": "storyboard", "render": "preview"}` — the gate is checked *before* that stage runs.
- `run_pipeline` already takes the project lock for its whole duration, so two runs of one project cannot interleave.
- `videomaker.cache` — `STAGE_ORDER`, `stage_key`, `StageCache`, `ResponseCache`, `hash_inputs`.
- `videomaker.pipeline.assemble` — `SPECS`/`WIDE_SPEC`, `video_relpath`, `narration_relpath`, `segment_relpath`, `scene_timeline`, `SCENE_GAP_S` (in `pipeline.base`).
- `videomaker.pipeline.render` — `output_relpath`, `subtitles_filter`, `run_render`.
- `videomaker.media.ffmpeg` — `run_ffmpeg(args, *, cwd=None, on_progress=None)` where `on_progress` receives **output seconds** (already clamped at 0), `probe_duration`, `FFmpegError`.
- `videomaker.models` — `Project`, `Scene`, `SceneVisual`, `AssetRef`, `Aspect`, `Status`, `VisualKind`, `Motion`. `Project` has **no stored `status`**; it is always derived.
- Mock providers registered as `"mock"` for every kind; `provider_override(settings, "mock")` swaps the whole chain.

### Plan conventions

Same as M1: **Interfaces blocks are normative**, **test code is given in full where it defines behaviour**, implementation is prose except where subtle. Every task ends with `uv run pytest -q && uv run ruff check .` green. Ruff here flags more than the classic `E4,E7,E9,F` set — expect `C408`, `UP017`, `B008`, `F401`, `RUF`, `ISC`, `TRY004`, `FURB`, `UP047`.

**Note on `B008`:** FastAPI's idiomatic `def route(x = Depends(...))` and `Form(...)` defaults are function calls in argument defaults, which ruff flags. Configure a targeted per-file ignore in `pyproject.toml` (`[tool.ruff.lint.per-file-ignores]` for `src/videomaker/web/*`) rather than contorting the code or disabling the rule globally — and say so in the task that first hits it.

### Design decisions made up front

1. **One worker thread, one job at a time.** Rendering is CPU-saturating (M1 measured 70 s of encode in a 184 s run on 12 threads); a second concurrent job would make both slower and could exhaust RAM. A `queue.Queue` plus one daemon thread is the whole scheduler. Deliberately no `asyncio` in the pipeline — the stages are synchronous by design (M1 constraint).
2. **Request handlers never run stages.** They enqueue and redirect. This keeps every request fast and means a blocking `fcntl.flock` can never hang the event loop.
3. **`derive_status` is the single source of truth for what the UI shows.** No page stores or caches a status. If the UI and the CLI ever disagree, that is a bug in `derive_status`, not something to paper over in a template.
4. **The 480p preview is an artefact, not a `STAGE_ORDER` stage.** Adding a stage would change `derive_status`, `STATUS_AFTER`, the golden-path test and M1's proven cache scoping. It gets its own cache key (`preview:wide`) outside `STAGE_ORDER`.
5. **Scene ids are stable and are not positions.** `s01…` are assigned once. Reordering changes list order, never ids — otherwise every downstream cache key would shift and reordering two scenes would re-render the whole video.

---

### Task 1: Web dependencies, app skeleton, and `videomaker serve`

**Files:**
- Create: `src/videomaker/web/__init__.py`, `src/videomaker/web/app.py`
- Modify: `pyproject.toml` (deps + ruff per-file-ignores), `src/videomaker/cli.py` (add `serve`)
- Test: `tests/unit/test_web_app.py`

**Interfaces:**
- Produces: `create_app(settings: Settings | None = None, *, providers: str | None = None) -> FastAPI` — an app factory, so tests build isolated instances rather than importing a module-level singleton. `app.state` carries `settings`, `store`, and (from Task 3) `worker`.
- `GET /healthz` → `{"status": "ok", "version": __version__}`.
- CLI: `videomaker serve [--host 127.0.0.1] [--port 8000] [--providers mock] [--reload]`.

- [x] **Step 1: Add the dependencies**

```bash
uv add fastapi uvicorn jinja2 python-multipart
```

These go in the **main** dependency list, not an extra: the web UI is the primary review surface, and the spec's DoD is a browser workflow.

- [x] **Step 2: Write the failing tests**

```python
from fastapi.testclient import TestClient

from videomaker import __version__
from videomaker.config import Settings
from videomaker.web.app import create_app


def _client(tmp_path) -> TestClient:
    return TestClient(create_app(Settings(workspace_dir=tmp_path)))


def test_healthz(tmp_path):
    response = _client(tmp_path).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


def test_app_factory_isolates_state(tmp_path):
    a = create_app(Settings(workspace_dir=tmp_path / "a"))
    b = create_app(Settings(workspace_dir=tmp_path / "b"))
    assert a.state.store.workspace_dir != b.state.store.workspace_dir


def test_provider_override_reaches_app_state(tmp_path):
    app = create_app(Settings(workspace_dir=tmp_path), providers="mock")
    assert app.state.settings.provider_chains["llm"] == ["mock"]


def test_unknown_route_is_404(tmp_path):
    assert _client(tmp_path).get("/no-such-page").status_code == 404
```

- [x] **Step 3: Run tests to verify they fail**, then implement.

`create_app` builds the store from `settings.workspace_dir`, mounts nothing yet, and registers `/healthz`. Apply `provider_override` when `providers` is given.

- [x] **Step 4: Add the `serve` command**

`serve` calls `uvicorn.run` on the factory result. It must print a **warning** when `--host` is not a loopback address, naming the risk explicitly: no authentication, and the app can read and write any path the user can. Do not merely log it at debug level.

- [x] **Step 5: Configure ruff for FastAPI's idioms**

Add to `pyproject.toml`:

```toml
[tool.ruff.lint.per-file-ignores]
"src/videomaker/web/*" = ["B008"]  # FastAPI's Depends()/Form() defaults are call expressions
```

- [x] **Step 6: Verify and commit**

```bash
uv run pytest -q && uv run ruff check .
git add pyproject.toml uv.lock src/videomaker/web/ src/videomaker/cli.py tests/unit/test_web_app.py
git commit -s -m "feat: FastAPI app factory, /healthz, and videomaker serve"
```

---

### Task 2: Media serving with a path-traversal guard

**Files:**
- Create: `src/videomaker/web/media.py`
- Modify: `src/videomaker/web/app.py` (mount the route)
- Test: `tests/unit/test_web_media.py`

**Interfaces:**
- Produces: `safe_project_path(root: Path, relative: str) -> Path` (raises `ValueError` on escape) and `GET /media/{project_id}/{path:path}` returning a `FileResponse`.

**This task is security-critical and gets its own task for that reason.** The server has no authentication and serves files from disk by a user-supplied path. A traversal bug here reads arbitrary files from the machine. The guard must resolve symlinks (`Path.resolve()`) and confirm containment with `is_relative_to`, **not** with string prefix comparison — `/workspace/projects/a-evil` starts with `/workspace/projects/a` as a string but is a different directory.

- [x] **Step 1: Write the failing tests**

```python
import pytest

from videomaker.web.media import safe_project_path


@pytest.fixture
def root(tmp_path):
    project = tmp_path / "projects" / "demo"
    (project / "output").mkdir(parents=True)
    (project / "output" / "final_wide.mp4").write_bytes(b"video")
    (tmp_path / "secret.txt").write_text("do not serve me")
    return project


def test_allows_a_file_inside_the_project(root):
    assert safe_project_path(root, "output/final_wide.mp4").read_bytes() == b"video"


@pytest.mark.parametrize(
    "attack",
    [
        "../../secret.txt",
        "../secret.txt",
        "output/../../../secret.txt",
        "/etc/passwd",
        "//etc/passwd",
        "output/./../../secret.txt",
        "....//....//secret.txt",
    ],
)
def test_rejects_traversal(root, attack):
    with pytest.raises(ValueError):
        safe_project_path(root, attack)


def test_rejects_a_symlink_pointing_outside(root, tmp_path):
    (root / "output" / "escape.txt").symlink_to(tmp_path / "secret.txt")
    with pytest.raises(ValueError):
        safe_project_path(root, "output/escape.txt")


def test_rejects_a_sibling_directory_sharing_a_name_prefix(root, tmp_path):
    evil = tmp_path / "projects" / "demo-evil"
    evil.mkdir(parents=True)
    (evil / "loot.txt").write_text("nope")
    # String-prefix containment checks pass this; only real path containment fails it.
    with pytest.raises(ValueError):
        safe_project_path(root, "../demo-evil/loot.txt")


def test_missing_file_raises_file_not_found(root):
    with pytest.raises(FileNotFoundError):
        safe_project_path(root, "output/nope.mp4")
```

Add route-level tests too: a traversal attempt through `GET /media/...` must return **404** (never 500, and never the file), and a valid request must return the bytes with a sensible `content-type`.

- [x] **Step 2: Run tests to verify they fail**, then implement.

Note the URL-decoded path is what reaches the handler; Starlette's `{path:path}` converter does not sanitise. Reject absolute inputs before joining.

- [x] **Step 3: Verify and commit**

```bash
git commit -s -m "feat: path-traversal-guarded media serving"
```

---

### Task 3: Job worker, queue and progress state

**Files:**
- Create: `src/videomaker/web/worker.py`
- Modify: `src/videomaker/web/app.py` (start/stop the worker on lifespan)
- Test: `tests/unit/test_web_worker.py`

**Interfaces:**
- Produces:
  - `JobState` dataclass: `project_id: str`, `kind: str`, `state: str` (`"queued" | "running" | "done" | "failed" | "blocked"`), `stage: str = ""`, `progress: float = 0.0` (0–1), `message: str = ""`, `error: str = ""`, `started_at: float | None`, `finished_at: float | None`.
  - `JobQueue` with `submit(project_id, kind, fn) -> None`, `state_for(project_id) -> JobState | None`, `is_busy() -> bool`, `start()`, `stop(timeout=5.0)`.
  - Job kinds in M2: `"run"` (advance the pipeline), `"preview"` (Task 11), `"revoice"` (one scene), `"research"` (one scene's stock search).
- The queue is **bounded** (`maxsize=32`) so a runaway client cannot exhaust memory; `submit` on a full queue raises `JobQueueFull`.
- `submit` is a no-op returning the existing state if that project already has a queued or running job — one job per project, and one job overall.

**Thread safety is the whole point of this task.** `JobState` is mutated by the worker thread and read by request handlers. Guard the state dict with a `threading.Lock` and hand out **copies** (`dataclasses.replace`) so a handler can never observe a half-updated record. Do not rely on the GIL.

- [x] **Step 1: Write the failing tests**

Cover: a submitted job runs and reaches `done`; `state_for` returns `None` for an unknown project; a raising job lands in `failed` with the message captured and the worker **still alive** for the next job; `GateBlocked` maps to `blocked`, not `failed`; a second submit for a busy project returns the existing state without enqueuing twice; two different projects queue and run sequentially, never concurrently (assert with a shared counter that never exceeds 1); `stop()` joins the thread; a full queue raises `JobQueueFull`; progress callbacks update `progress` monotonically.

Use `threading.Event` to synchronise the tests, never `sleep` polling with a bare timeout — a sleep-based test is flaky on a loaded CI runner.

- [x] **Step 2: Run tests to verify they fail**, then implement.

The worker translates M1's exceptions: `GateBlocked` → `state="blocked"` with `GATE_REVIEW[gate]` as the message (this is a normal outcome, not an error); `StageFailed` → `state="failed"` with the stage name and cause; anything else → `failed` with the exception text. It must never let an exception kill the thread.

Wire `on_stage` from `run_pipeline` to update `stage` and a coarse progress (stage index / total). Task 12 refines render progress using `run_ffmpeg`'s `on_progress`.

- [x] **Step 3: Verify and commit**

```bash
git commit -s -m "feat: single-worker job queue with thread-safe progress state"
```

---

### Task 4: Templates, vendored htmx, static assets

**Files:**
- Create: `src/videomaker/web/templates/{base.html,_nav.html}`, `src/videomaker/web/static/style.css`, `src/videomaker/web/static/vendor/htmx.min.js`
- Modify: `src/videomaker/web/app.py` (Jinja2 env + static mount), `NOTICE.md`, `pyproject.toml` (package the web assets)
- Test: `tests/unit/test_web_templates.py`

**Interfaces:**
- Produces: `templates: Jinja2Templates` on `app.state`, `GET /static/*` serving the committed assets.

- [x] **Step 1: Vendor htmx**

```bash
mkdir -p src/videomaker/web/static/vendor
curl -fsSL https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js -o src/videomaker/web/static/vendor/htmx.min.js
ls -lh src/videomaker/web/static/vendor/htmx.min.js
sha256sum src/videomaker/web/static/vendor/htmx.min.js
```

Verify it is roughly 14–50 KB and starts with a JS comment or `(function`. **Record the exact version and SHA-256 in `NOTICE.md`** alongside its licence (htmx is BSD-2-Clause) — this is an AGPL project and the licensing footprint has to stay honest. If the fetch fails, report it rather than substituting a CDN `<script src>`; the no-CDN rule is deliberate (offline use, and no third-party request from a local tool).

- [x] **Step 2: Package the assets**

`[tool.hatch.build.targets.wheel]` currently packages `src/videomaker`, which includes the templates and static files since they live under the package — **verify this** with `uv build && python -c "import zipfile; print([n for n in zipfile.ZipFile(sorted(__import__('pathlib').Path('dist').glob('*.whl'))[-1]).namelist() if 'templates' in n or 'static' in n])"` and fix it if they are missing. (Contrast with `templates/` at the repo root, which is the *niche template* directory and is a known M6 packaging gap — different thing, same trap.)

- [x] **Step 3: Write base.html and the stylesheet**

`base.html` provides the document shell, loads `htmx.min.js` from `/static/vendor/`, and defines blocks `title`, `content`, `scripts`. Keep the CSS to one hand-written file — no framework, no build. Aim for a legible single-column layout that works at 1280 px and on a laptop screen; this is a personal review tool, not a marketing site.

Tests assert: `/static/vendor/htmx.min.js` returns 200 with a JS content type; the rendered base contains no `http://` or `https://` external asset reference (a regression guard on the no-CDN rule).

- [x] **Step 4: Verify and commit**

```bash
git commit -s -m "feat: base templates, stylesheet, vendored htmx (BSD-2, recorded in NOTICE)"
```

---

### Task 5: Project list and creation (`/`)

**Files:**
- Create: `src/videomaker/web/routes/__init__.py`, `src/videomaker/web/routes/projects.py`, `templates/index.html`
- Modify: `src/videomaker/web/app.py`
- Test: `tests/unit/test_web_projects.py`

**Interfaces:**
- `GET /` — lists every project with its derived status; form to create one.
- `POST /projects` — form fields `topic`, `template`, `minutes`, `voice`; creates via `ProjectStore.create`, enqueues a `run --until script` job, redirects (303) to `/projects/{id}`.

Tests: the list shows created projects with correct derived statuses; creating redirects and the project exists on disk; an empty topic is rejected with a form error (422 or a re-rendered form, not a 500); the template dropdown is populated from `list_templates()`; two projects with the same topic get distinct ids.

- [x] **Step 1–3: TDD as above, then commit**

```bash
git commit -s -m "feat: project list and creation page"
```

---

### Task 6: Project dashboard (`/projects/{id}`)

**Files:**
- Create: `templates/project.html`, `templates/_status.html`, `templates/_job.html`
- Modify: `src/videomaker/web/routes/projects.py`
- Test: `tests/unit/test_web_dashboard.py`

**Interfaces:**
- `GET /projects/{id}` — a stepper showing the seven stages, which are current, the three gate states, and the active job if any.
- `GET /projects/{id}/job` — the `_job.html` **partial**, polled by htmx every 1.5 s (`hx-trigger="every 1500ms"`).
- `POST /projects/{id}/advance` — enqueue a `run` job to the next gate; redirect back.

The polling partial must stop polling once the job finishes — return the partial with the `hx-trigger` attribute omitted when `state` is terminal, so a finished page does not poll forever. Test this explicitly: it is the difference between an idle tab and one making 40 requests a minute for the rest of the day.

Also test: a 404 for an unknown project id; the stepper marks exactly the stages `stage_is_current` reports; gate rows show approved/pending correctly.

- [x] **Step 1–3: TDD as above, then commit**

```bash
git commit -s -m "feat: project dashboard with stepper and htmx job polling"
```

---

### Task 7: Gate 1 — script review (`/projects/{id}/script`)

**Files:**
- Create: `src/videomaker/web/routes/script.py`, `templates/script.html`
- Test: `tests/unit/test_web_script_gate.py`

**Interfaces:**
- `GET /projects/{id}/script` — one textarea per scene (narration) plus a visual-query input.
- `POST /projects/{id}/scenes/{scene_id}` — save narration and/or `visual.query`; returns the updated scene row partial (htmx), 200.
- `POST /projects/{id}/approve/script` — stamp `Approvals.script`, enqueue a run to the next gate, redirect.

**The saving path must go through the lock and must be a no-op when nothing changed** — writing an identical narration must not touch `project.json` and must not invalidate the voice cache. Test that explicitly by comparing `stages.json` before and after a no-op save: this is what keeps the M1 caching promise intact under a UI that autosaves on every keystroke pause.

Tests: editing narration persists and invalidates only that scene's voice/align units; a no-op save changes nothing; approving stamps the timestamp and enqueues; approving an already-approved gate is idempotent; editing after approval clears the downstream approvals (M1's `clear_stale_approvals` already does this — assert the UI surfaces it).

- [x] **Step 1–3: TDD as above, then commit**

```bash
git commit -s -m "feat: gate 1 script review with per-scene editing"
```

---

### Task 8: Scene operations — split, merge, reorder, delete

**Files:**
- Create: `src/videomaker/scenes.py` (pure functions), `src/videomaker/web/routes/scenes.py`
- Modify: `templates/script.html`
- Test: `tests/unit/test_scene_ops.py`, `tests/unit/test_web_scene_ops.py`

**Interfaces:**
- Pure, in `scenes.py`: `split_scene(project, scene_id, at_word: int) -> Project`, `merge_scenes(project, first_id, second_id) -> Project`, `reorder_scenes(project, ordered_ids: list[str]) -> Project`, `delete_scene(project, scene_id) -> Project`.
- Routes: `POST /projects/{id}/scenes/{scene_id}/split|merge|delete`, `POST /projects/{id}/scenes/reorder`.

**Scene ids are stable and are not positions.** This is the task where that invariant is easy to break. `reorder_scenes` changes list order only; every id keeps its artefacts and its cache entries, so reordering two scenes must re-encode nothing except what genuinely depends on order (the concat list and the final render — not the per-scene segments). Assert that.

`split_scene` gives the new half a fresh id from `next_scene_id()` and must invalidate the original's voice/align/visuals. `merge_scenes` keeps the **first** id and must delete the second's artefacts rather than orphaning them on disk.

Tests (pure functions, no HTTP): ids stay stable across a reorder; split produces two scenes whose narrations concatenate back to the original; split at word 0 or past the end is rejected; merge concatenates in order and drops the second id; delete removes artefacts; every operation leaves `project.scenes` ids unique; reordering does not change any per-scene stage hash.

- [x] **Step 1–3: TDD as above, then commit**

```bash
git commit -s -m "feat: split, merge, reorder and delete scenes with stable ids"
```

---

### Task 9: Gate 2 — storyboard (`/projects/{id}/storyboard`)

**Files:**
- Create: `src/videomaker/web/routes/storyboard.py`, `templates/storyboard.html`, `templates/_scene_card.html`
- Test: `tests/unit/test_web_storyboard.py`

**Interfaces:**
- `GET /projects/{id}/storyboard` — a card per scene: chosen visual (video or image preview via `/media/...`), the other candidates as thumbnails, a motion select, and a re-voice button.
- `POST /projects/{id}/scenes/{scene_id}/choose` — form field `candidate_index`; sets `visual.chosen` from `visual.candidates`, returns the updated card.
- `POST /projects/{id}/scenes/{scene_id}/motion` — set `Motion`.
- `POST /projects/{id}/scenes/{scene_id}/revoice` — enqueue a `revoice` job for one scene.
- `POST /projects/{id}/approve/storyboard`.

**The DoD lives here:** *editing one scene at storyboard re-generates only that scene*. Swapping a candidate must invalidate that scene's assemble unit and the final render — and nothing else. Prove it the way M1 did: count `run_ffmpeg` invocations across the re-run and assert exactly one segment re-encode, plus mtime witnesses on the untouched segments.

Note `visual.candidates[1:]` have `local_path=""` (M1 downloads only the chosen one), so choosing a different candidate must download it first. Test the not-yet-downloaded path.

- [x] **Step 1–3: TDD as above, then commit**

```bash
git commit -s -m "feat: gate 2 storyboard with candidate swap, motion and re-voice"
```

---

### Task 10: Live stock search with a quota indicator

**Files:**
- Modify: `src/videomaker/web/routes/storyboard.py`, `templates/_scene_card.html`
- Test: `tests/unit/test_web_stock_search.py`

**Interfaces:**
- `POST /projects/{id}/scenes/{scene_id}/search` — form field `query`; runs a stock search through the provider chain, replaces `visual.candidates`, returns the updated card.
- `GET /projects/{id}/quota` — a partial showing remaining headroom per provider from `QuotaTracker.remaining`.

**Show the user what a search costs before they spend it.** Pexels' soft budget is 190/hour and browsing burns it fast; the indicator is what keeps a review session from silently hitting the wall. Searches go through M1's `ResponseCache(ttl_days=7)`, so repeating a query must consume **no** quota — test that a repeated identical search issues zero HTTP requests and leaves `quota.json` byte-identical.

Also test: `QuotaExceeded` renders a clear message in the card rather than a 500; a search with an empty query is rejected client-side and server-side.

- [x] **Step 1–3: TDD as above, then commit**

```bash
git commit -s -m "feat: live stock search with remaining-quota indicator"
```

---

### Task 11: 480p preview artefact

**Files:**
- Create: `src/videomaker/preview.py`
- Test: `tests/unit/test_preview.py`

**Interfaces:**
- Produces: `PREVIEW_HEIGHT = 480`, `preview_relpath(aspect) -> str` (→ `build/preview_wide.mp4`), `build_preview(project, deps, *, on_progress=None) -> Path`.

**Deliberately not a `STAGE_ORDER` stage** (design decision 4): adding one would shift `derive_status`, `STATUS_AFTER`, the golden-path test and M1's proven cache scoping. It is cached under its own key `preview:wide` in the same `StageCache`, hashed on the assembled video's digest plus the caption file's digest.

Reuse the render filter graph at 480p with `-preset ultrafast`, running with `cwd=<project dir>` and **relative** paths — the M0 subtitle-path finding applies identically here.

Tests: the preview is 854×480 (or whatever the spec's aspect maths gives at height 480, asserted via ffprobe); a second call with nothing changed re-encodes nothing; changing the captions invalidates it; `on_progress` fires.

- [x] **Step 1–3: TDD as above, then commit**

```bash
git commit -s -m "feat: cached 480p preview render for the review gate"
```

---

### Task 12: Gate 3 — preview and render (`/projects/{id}/preview`, `/render`)

**Files:**
- Create: `src/videomaker/web/routes/render.py`, `templates/preview.html`, `templates/render.html`
- Test: `tests/unit/test_web_render_gate.py`

**Interfaces:**
- `GET /projects/{id}/preview` — plays `build/preview_wide.mp4` via `/media/...`; a button enqueues a `preview` job when it is missing or stale.
- `POST /projects/{id}/approve/preview` — stamp, enqueue the final render, redirect to `/render`.
- `GET /projects/{id}/render` — progress page polling `/job`; on completion, plays `output/final_wide.mp4` and shows the file path.

**Refine progress here.** Wire `run_ffmpeg`'s `on_progress` (output seconds) through the render stage into `JobState.progress` as a fraction of the known timeline duration — `scene_timeline` already gives the total. A progress bar that only moves between stages is close to useless for a 40-second encode.

Tests: the preview page 404s cleanly when the project has not been assembled; approving enqueues the render; the render page shows a playable file when done; progress reaches 1.0; a failed render surfaces the FFmpeg stderr tail rather than a bare 500.

- [x] **Step 1–3: TDD as above, then commit**

```bash
git commit -s -m "feat: gate 3 preview approval and render progress page"
```

---

### Task 13: Browser-path integration test

**Files:**
- Create: `tests/integration/test_web_golden_path.py`

**Interfaces:**
- Produces: the regression guarding the M2 DoD — *a full project via the browser*.

Drive the whole workflow over HTTP with `TestClient` and `--providers mock` plus real FFmpeg, mirroring M1's golden path:

1. `POST /projects` → project created, script job runs to gate 1.
2. `GET /script` shows the scenes; `POST` an edit to one scene; approve gate 1.
3. Wait for the job (poll `/job` with a real timeout, exactly as a browser would); assert it reaches gate 2.
4. `GET /storyboard`; swap a candidate on scene 2; approve gate 2.
5. Assert **exactly one** segment re-encode across that swap — the DoD sentence, made executable.
6. Approve gate 3; assert `output/final_wide.mp4` exists and ffprobe reports 1920×1080 h264+aac.

Make it fail first for the right reason (e.g. temporarily disable `clear_stale_approvals` or the per-scene cache key) and report what you did.

Keep it under ~90 s. Add it to CI — the workflow already runs `uv run pytest -q` over `tests/`, so confirm no CI change is needed rather than assuming.

- [x] **Step 1–3: TDD as above, then commit**

```bash
git commit -s -m "test: end-to-end browser workflow with mock providers and real ffmpeg"
```

---

### Task 14: M2 definition-of-done verification

**Files:**
- Create: `docs/superpowers/spike-results-m2.md`

- [x] **Step 1: Drive a real project through the browser**

Start `uv run videomaker serve` with **real** providers. Using the `browser-automation` skill (or `claude-in-chrome` if it is available), actually load the pages: create a project, edit a scene at gate 1, swap a visual at gate 2, watch the preview, approve, and download the render. Capture screenshots of all three gate pages and report console errors and failed network requests — a page that 500s in the browser but passes `TestClient` is a real possibility, since `TestClient` does not execute JavaScript.

- [x] **Step 2: Verify the DoD sentence literally**

Edit one scene's visual at the storyboard gate and confirm from the logs and mtimes that exactly one scene re-generated.

- [x] **Step 3: Full battery**

```bash
uv run pytest -q && uv run ruff check .
uv run videomaker doctor
uv run videomaker run <id> --yes     # the CLI must still work unchanged
```

- [x] **Step 4: Record results and commit**

Write `docs/superpowers/spike-results-m2.md`: wall times per gate, any console errors, screenshots referenced, and follow-ups for M3. Carry forward any M1 follow-up that is still open.

```bash
git commit -s -m "docs: M2 complete — three review gates in the browser"
```

---

## Self-review notes

- **Spec coverage (M2 scope)**: FastAPI+Jinja2+vendored htmx, no npm ✓ (T1, T4); worker thread + `queue.Queue`, one job ✓ (T3); polling progress partial at 1.5 s ✓ (T6, T12); routes `/`, `/projects/{id}`, `/script`, `/storyboard`, `/preview`, `/render`, `/media/{id}/{path}` guarded, `/healthz` ✓ (T1–T12); 480p preview ✓ (T11); binds 127.0.0.1 with a warning on `0.0.0.0` ✓ (T1).
- **Deliberately deferred**: crop-focus slider with the 9:16 overlay, `in_short` toggle, side-by-side dual-aspect previews and the music picker are all **M3** (they are listed on the spec's storyboard/preview routes but every one of them is a vertical-or-music feature). Metadata copy and upload on `/render` are **M5**. The `Scene.in_short` and `SceneVisual.crop_focus_x` fields already exist from M1 and stay untouched.
- **Riskiest tasks**: T3 (threading — a data race here shows up as a flaky test, so the state dict is lock-guarded and handed out as copies) and T2 (security — no auth means a traversal bug is a machine-wide file read, hence its own task and a symlink case).
- **The DoD sentence is executable**: "editing one scene at storyboard re-generates only that scene" is asserted in T9 by counting `run_ffmpeg` calls, and again end to end in T13.
- **Sequencing**: T1→T2→T3→T4 are foundation and mostly sequential (T2 is independent of T3 and can run beside it). T5→T6 are sequential. T7, T9, T11 are independent of one another once T6 lands; T8 depends on T7; T10 depends on T9; T12 depends on T11. T13 needs everything; T14 last.
