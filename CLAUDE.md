# Longhand — working agreement for coding agents

Read this before touching anything. It is short on purpose; the reasoning lives
in `docs/`, and the modules carry it in their own docstrings.

**What this is.** `videomaker` (product name **Longhand**), a local-first,
human-in-the-loop AI video studio. A topic becomes a curated 16:9 video and a
9:16 Short from one project, using free/open models. The thesis is *curation
over automation* — three mandatory human gates. Milestones M0–M6 shipped.

**What is being built now.** The multimodal reader: a work in a library, read as
text, as a plain-language brief, or as narration. See
`docs/superpowers/plans/2026-09-09-mvp-reader.md` — that plan is the current
task list, and it is normative. `docs/multimodal-reader-design.md` is the *why*.

## Commands

```bash
uv sync                      # runtime deps; add --extra ml for Kokoro/Whisper
uv run pytest -q             # the whole suite; must be green
uv run ruff check .          # must be clean
uv run videomaker doctor     # environment check: OK/WARN fine, no FAIL
uv run videomaker --help     # the CLI surface
```

A task is done when `uv run pytest -q && uv run ruff check .` is green and the
task's own tests exist and fail if the behaviour is removed. **Mutation-test
every guarantee a test claims to hold** — delete the check, watch the test fail,
put it back. M1–M3 did this on every task and it caught a vacuous test each time.

## Hard rules

1. **$0 cost, offline-first.** No paid service is required to run anything. No
   PyTorch. No MoviePy. Python `>=3.12,<3.13`. Linux x86_64 is primary.
2. **No new runtime dependency** without saying in the commit message what it
   costs and what it replaces. Import-time cost matters; the CLI must stay fast.
3. **The project ships no audio files and no scripture text.** Both are
   licensing decisions and both are binding — see `docs/audio-design.md` and
   `docs/multimodal-reader-design.md` §5. `assets/` and `workspace/` are the
   user's own.
4. **Never bundle content whose licence you cannot state in one sentence.** The
   importer enforces this in code; do not add a `--force`.
5. **AGPL-3.0-only.** Sign every commit: `git commit -s`.
6. No absolute URLs in web templates. Nothing loads from a CDN — a local tool
   must work with no network and must not announce page views to third parties.
   `tests/unit/test_web_templates.py` fails the build if one appears.
7. No hard-coded colour or spacing value in `web/static/style.css` outside the
   token block at the top. `tests/unit/test_palette_contrast.py` reads the
   tokens back out of the file and asserts their contrast ratios.

## Things that will break the pipeline if you touch them

These are not style preferences. Each one has already cost a day.

- **`cache.STAGE_ORDER` is append-only.** `runner.derive_status` returns the
  status of the last *current* stage, so appending cannot lower any project's
  status — prepending drops every project on disk to `new`. The reader is
  additive: it adds no stage. Artefacts that are not stages (`preview.py`,
  `audiobook`) get their own key in the same `StageCache` and stay outside
  `STAGE_ORDER`.
- **`Template.script_fingerprint()` is `model_dump_json` minus two fields.**
  Adding *any* field to `Template` moves every existing template's fingerprint,
  stales `script:all` for every project on disk, and `run_script` then replaces
  `project.scenes` **wholesale** — taking every voiced take, chosen shot and
  approval with it. M3 Task 22 walked into this once. If a field must be added,
  change the fingerprint to an explicit field list that omits any field still at
  its default, and pin the current hashes as literals in a test.
- **New `Project` / `Scene` fields are optional with a falsy default**, so every
  `project.json` already on disk loads unchanged. `folder`, `thumbnail_text`,
  `alt_queries` and `beat` all follow this; match them.
- **FFmpeg runs with `cwd=<project>` and relative paths.** An absolute `.ass`
  path containing a colon breaks the filter parser (M0 finding).
- **`languages.py` imports nothing from the rest of the package**, and must keep
  not doing so — `project`, `doctor`, `media.fonts` and the web layer all read it.

## House style

- **Docstrings and comments explain *why*, and record what was measured** — not
  what the line does. Where a decision was made against an obvious alternative,
  say what the alternative was and why it lost. Where a number appears, say where
  it came from. Read `pipeline/script.py` and `runner.py` for the register.
- British spelling in prose (`licence`, `normalised`, `behaviour`).
- Ruff runs well beyond the defaults here: `C408`, `UP017`, `B008`
  (per-file-ignored under `web/`), `F401`, `RUF`, `ISC`, `TRY004`, `FURB`,
  `UP047`. Line length 100.
- Tests live under `tests/unit/`, `tests/integration/`, `tests/quality/`. The
  whole suite is offline; the golden path drives the CLI with `--providers mock`.
  Mark anything needing model weights or live quota `@pytest.mark.slow`.
- One commit per plan task, message in the imperative, and tick the task's
  checkbox in the plan file in the same commit.

## When you are stuck

Do not invent a design. The plan's **Interfaces blocks are normative** — if the
plan does not say, and the answer is not derivable from an existing module,
leave a `TODO(owner):` with the question and move to the next task rather than
guessing. A wrong guess that passes tests is more expensive than a gap.
