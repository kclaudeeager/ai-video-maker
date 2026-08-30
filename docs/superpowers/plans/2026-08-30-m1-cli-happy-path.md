# M1 — CLI Happy Path (Wide) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `videomaker new "how ssds work" -t tech_explainer && videomaker run <id> --yes` produces a playable `output/final_wide.mp4` with burned captions — and an immediate re-run completes in under 10 seconds without spending a single unit of free-tier quota.

**Architecture:** A pydantic data model (`models.py`) persisted as `project.json` by an atomic-write `ProjectStore` (`project.py`). A content-hash engine (`cache.py`) decides which stage units are stale, so editing one scene re-runs only that scene. Seven pipeline stages (script → voice → align → visuals → captions → assemble → render) each read and write the project, guarded by the hash engine. Providers sit behind six sync ABCs with a decorator registry and ordered fallback chains; a mock implementation of every ABC makes the whole pipeline testable without network or quota.

**Tech Stack:** Everything from M0, plus: `difflib` (snap-to-script), `Pillow` (M3 — not M1). Providers: Groq + Gemini (LLM), kokoro-onnx (TTS), faster-whisper (STT), Pexels (stock), Cloudflare Workers AI (image).

## Global Constraints

- Everything from the M0 plan still holds: **$0 cost**, **Python `>=3.12,<3.13`**, **Linux x86_64 primary**, **no PyTorch**, **no MoviePy** (FFmpeg/ffprobe via subprocess only), **AGPL-3.0-only**, **sign off every commit** (`git commit -s`), working directory is the repo root.
- **Scope is wide (1920×1080) only.** Vertical/9:16, karaoke captions, music, ducking, thumbnails and `clean` are **M3**. Metadata generation and upload are **M5**. The web UI is **M2**. Do not build them here — but do not design them out either (see "Forward compatibility" below).
- **Never spend quota twice for the same input.** Every provider call goes through the response cache. The golden-path test must prove a second run makes zero provider calls.
- **Providers are lazily imported** inside functions, exactly as M0 does for `kokoro_onnx` and `faster_whisper`, so the scaffold and `--providers mock` work without the `ml` extra installed.
- **Pipeline runs in a worker thread** (M2 needs this), so all provider ABCs are **synchronous** and use `httpx.Client`. No `async` anywhere in the pipeline.

### Findings from M0 this plan must honour

These were measured on this machine and recorded in `docs/superpowers/spike-results-m0.md`. Each is load-bearing:

1. **Never hardcode a Groq model id.** `llama-3.3-70b-versatile` and `llama-3.1-8b-instant` no longer exist on Groq. Resolve the catalogue at runtime against a preference list, and **raise `ProviderConfigError` if nothing matches** — never silently fall back to whatever id sorts first (that picked `allam-2-7b`, 4k context, and produced a factually wrong script that looked like a content bug, not a config bug).
2. **Build `WhisperModel` once and reuse it.** Construction dominates STT wall time (RTF ≈ 1.02 warm, almost all of it model init). Per-scene construction would make alignment cost realtime.
3. **Whisper word timestamps are zero-gap** (`word.start == previous word.end`). The ASS writer must inset chunk boundaries or captions will be edge-to-edge.
4. **Kokoro emits float32 @ 24 kHz mono.** `soundfile.write` silently downcasts to 16-bit PCM. Pass an explicit subtype; resample at mix time (M3), not at TTS time.
5. **Flux returns 1024×1024 squares.** Cropping to 16:9 discards ~⅓ of the frame. Ask Flux for composition headroom in the prompt and pad-or-crop deliberately — never assume the image matches the timeline aspect.
6. **Pin `@cf/black-forest-labs/flux-1-schnell`** (~58 neurons per 1024² at 4 steps, ~170 images/day free). Leonardo models cost 530–636 neurons *per tile* — ~100× more. Workers AI auto-bills past the free cap rather than failing, so the quota tracker must stop us.
7. **Per-scene narration gives exact per-scene durations**, so visual segments cut to audio with no alignment guesswork. Keep this shape.
8. **`ffmpeg` on PATH does not imply `ffprobe`** — probe both (M0's `doctor` already does).
9. **Anonymous HF Hub downloads are rate-limited.** Support an optional `HF_TOKEN`.

### Plan conventions

Unlike the M0 plan, **not every step carries full implementation code** — M1 is far larger, and spelling out every line would bury the design. Instead:

- **Interfaces blocks are normative.** Signatures, types and names are contracts between tasks. Do not change them without updating every downstream task in this file.
- **Test code is given in full.** The tests are the specification — implement whatever makes them pass, in the style of the surrounding code.
- **Implementation code is given only where it is subtle** (hash engine, ASS writer, snap-to-script, FFmpeg filter graphs). Elsewhere, prose describes the required behaviour.
- Every task ends green: `uv run pytest -q && uv run ruff check .`.

### Forward compatibility (design for, do not build)

- `OutputSpec` is keyed by `Aspect`; M1 only ever populates `Aspect.WIDE`, but nothing may assume a single aspect.
- `Scene.in_short` exists in the model from day one (M3 uses it); M1 defaults it to `True` and ignores it.
- Caption layout constants are per-aspect from the start — M1 fills in wide, M3 adds vertical. Vertical must never be derived by scaling wide.
- Stage functions take `(project, store, deps)` and return the mutated project, so M2's web worker can call them one unit at a time.

---

### Task 1: Data model

**Files:**
- Create: `src/videomaker/models.py`
- Test: `tests/unit/test_models.py`
- Modify: `tests/` layout — create `tests/unit/` and move the five existing M0 test files into it; add `tests/unit/__init__.py` is **not** needed (pytest rootdir config already handles it).

**Interfaces:**
- Produces: `Aspect` (`StrEnum`: `WIDE="wide"`, `VERTICAL="vertical"`), `Status` (`StrEnum`: `NEW`, `SCRIPT_READY`, `VOICED`, `STORYBOARD_READY`, `PREVIEW_READY`, `RENDERED`), `VisualKind` (`StrEnum`: `AUTO`, `STOCK_VIDEO`, `STOCK_PHOTO`, `AI_IMAGE`), `Motion` (`StrEnum`: `PAN`, `ZOOM`, `NONE`).
- `WordTiming(word: str, start_s: float, end_s: float)`.
- `AssetRef(provider: str, source_id: str, source_url: str, local_path: str, width: int, height: int, duration_s: float | None, attribution: str, license: str)` — `local_path` is **always relative to the project folder**.
- `StockResult(provider, source_id, source_url, preview_url, download_url, width, height, duration_s, attribution, license)`.
- `SceneVisual(query: str, kind: VisualKind = AUTO, chosen: AssetRef | None = None, candidates: list[AssetRef] = [], motion: Motion = PAN, crop_focus_x: float = 0.5, trim_start_s: float = 0.0)`.
- `Scene(id: str, narration: str, visual: SceneVisual, audio_path: str | None, duration_s: float | None, words: list[WordTiming] = [], in_short: bool = True, locked: bool = False, error: str | None = None)`.
- `Approvals(script: datetime | None, storyboard: datetime | None, preview: datetime | None)`.
- `OutputSpec(aspect: Aspect, width: int, height: int, scene_ids: list[str], video_path: str | None)`.
- `Project(id: str, topic: str, template: str, language: str = "en", voice: str = "af_heart", target_minutes: float = 2.0, created_at: datetime, approvals: Approvals, scenes: list[Scene], outputs: dict[Aspect, OutputSpec])` with helpers `scene_by_id(sid) -> Scene` and `next_scene_id() -> str`.
- Consumed by every later task.

- [ ] **Step 1: Reorganise the test tree**

`git mv` the five M0 test files into `tests/unit/`. Run `uv run pytest -q` and confirm 16 still pass (pyproject's `testpaths = ["tests"]` already recurses). Commit this move on its own so the later diff stays readable.

- [ ] **Step 2: Write the failing tests**

`tests/unit/test_models.py`:

```python
from datetime import datetime, timezone

import pytest

from videomaker.models import (
    Aspect,
    AssetRef,
    Motion,
    Project,
    Scene,
    SceneVisual,
    VisualKind,
    WordTiming,
)


def _project(**kw) -> Project:
    defaults = dict(
        id="how-ssds-work",
        topic="how ssds work",
        template="tech_explainer",
        created_at=datetime(2026, 8, 30, tzinfo=timezone.utc),
        scenes=[
            Scene(id="s01", narration="First.", visual=SceneVisual(query="ssd")),
            Scene(id="s02", narration="Second.", visual=SceneVisual(query="nand")),
        ],
    )
    return Project(**{**defaults, **kw})


def test_defaults_are_sane():
    p = _project()
    assert p.language == "en"
    assert p.voice == "af_heart"
    assert p.scenes[0].in_short is True
    assert p.scenes[0].visual.kind is VisualKind.AUTO
    assert p.scenes[0].visual.motion is Motion.PAN
    assert p.scenes[0].visual.crop_focus_x == 0.5
    assert p.approvals.script is None


def test_round_trips_through_json():
    p = _project()
    p.scenes[0].words = [WordTiming(word="First", start_s=0.0, end_s=0.4)]
    p.scenes[0].visual.chosen = AssetRef(
        provider="pexels", source_id="123", source_url="https://x/1",
        local_path="scenes/s01/asset.mp4", width=1920, height=1080,
        duration_s=6.0, attribution="A Photographer", license="Pexels",
    )
    restored = Project.model_validate_json(p.model_dump_json())
    assert restored == p


def test_scene_lookup_and_next_id():
    p = _project()
    assert p.scene_by_id("s02").narration == "Second."
    assert p.next_scene_id() == "s03"
    with pytest.raises(KeyError):
        p.scene_by_id("s99")


def test_outputs_keyed_by_aspect():
    p = _project()
    p.outputs[Aspect.WIDE] = p.outputs.get(Aspect.WIDE) or _wide(p)
    assert p.outputs[Aspect.WIDE].width == 1920
    # Aspect must survive a JSON round trip as a dict key.
    restored = Project.model_validate_json(p.model_dump_json())
    assert restored.outputs[Aspect.WIDE].scene_ids == ["s01", "s02"]


def _wide(p):
    from videomaker.models import OutputSpec

    return OutputSpec(
        aspect=Aspect.WIDE, width=1920, height=1080,
        scene_ids=[s.id for s in p.scenes],
    )


def test_crop_focus_is_bounded():
    with pytest.raises(ValueError):
        SceneVisual(query="x", crop_focus_x=1.5)
```

- [ ] **Step 3: Run tests to verify they fail**

`uv run pytest tests/unit/test_models.py -v` → FAIL (`ModuleNotFoundError: videomaker.models`).

- [ ] **Step 4: Implement models.py**

Use pydantic v2 `BaseModel`. Notes:
- `StrEnum` from `enum` (Python 3.12) so aspect keys serialise as plain strings.
- `crop_focus_x: float = Field(0.5, ge=0.0, le=1.0)`.
- `next_scene_id()` returns `f"s{max(existing)+1:02d}"`, or `"s01"` when empty.
- Give `Approvals`, `scenes`, `outputs` proper `default_factory` values — never mutable defaults.

- [ ] **Step 5: Run tests, then commit**

```bash
uv run pytest -q && uv run ruff check .
git add src/videomaker/models.py tests/unit/test_models.py
git commit -s -m "feat: pydantic data model for projects, scenes and outputs"
```

---

### Task 2: ProjectStore

**Files:**
- Create: `src/videomaker/project.py`
- Test: `tests/unit/test_project.py`

**Interfaces:**
- Consumes: `Project` (Task 1), `Settings` (M0 `config.py`).
- Produces: `slugify(topic: str) -> str`; `ProjectStore(workspace_dir: Path)` with `create(topic, template, **kw) -> Project`, `load(project_id) -> Project`, `save(project) -> None`, `list_ids() -> list[str]`, `path_for(project_id) -> Path`, `scene_dir(project, scene_id) -> Path`, and a context manager `lock(project_id)`.
- Layout created on `create()`: `<workspace>/projects/<id>/{scenes,audio,captions,build,output,cache}/`.
- `save()` is **atomic**: write `project.json.tmp` in the same directory, `os.replace` onto `project.json`. Never a partial file.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_project.py`:

```python
import json
from pathlib import Path

import pytest

from videomaker.project import ProjectStore, slugify


@pytest.fixture
def store(tmp_path) -> ProjectStore:
    return ProjectStore(tmp_path)


@pytest.mark.parametrize(
    "topic,expected",
    [
        ("how ssds work", "how-ssds-work"),
        ("  Why the Ocean is BLUE!  ", "why-the-ocean-is-blue"),
        ("C++ & Rust: a comparison", "c-rust-a-comparison"),
        ("émoji 🎬 test", "emoji-test"),
        ("a" * 90, "a" * 60),
    ],
)
def test_slugify(topic, expected):
    assert slugify(topic) == expected


def test_create_makes_folder_layout(store):
    p = store.create("how ssds work", "tech_explainer")
    root = store.path_for(p.id)
    assert (root / "project.json").exists()
    for sub in ("scenes", "audio", "captions", "build", "output", "cache"):
        assert (root / sub).is_dir()


def test_create_disambiguates_collisions(store):
    a = store.create("how ssds work", "tech_explainer")
    b = store.create("how ssds work", "tech_explainer")
    assert a.id == "how-ssds-work"
    assert b.id == "how-ssds-work-2"


def test_save_then_load_round_trips(store):
    p = store.create("topic here", "tech_explainer")
    p.topic = "changed"
    store.save(p)
    assert store.load(p.id).topic == "changed"


def test_save_is_atomic_and_leaves_no_temp_file(store):
    p = store.create("topic here", "tech_explainer")
    store.save(p)
    root = store.path_for(p.id)
    assert list(root.glob("*.tmp")) == []
    json.loads((root / "project.json").read_text())  # valid JSON, not truncated


def test_load_unknown_id_raises(store):
    with pytest.raises(FileNotFoundError):
        store.load("does-not-exist")


def test_list_ids_sorted(store):
    store.create("b topic", "tech_explainer")
    store.create("a topic", "tech_explainer")
    assert store.list_ids() == ["a-topic", "b-topic"]


def test_lock_is_reentrant_safe_across_processes(store, tmp_path):
    p = store.create("topic here", "tech_explainer")
    with store.lock(p.id):
        assert (store.path_for(p.id) / ".lock").exists()
    # Lock is released, so a second acquisition succeeds immediately.
    with store.lock(p.id):
        pass


def test_scene_dir_is_created_on_demand(store):
    p = store.create("topic here", "tech_explainer")
    d = store.scene_dir(p, "s01")
    assert d.is_dir() and d.name == "s01"
```

- [ ] **Step 2: Run tests to verify they fail**, then implement.

Implementation notes:
- `slugify`: NFKD-normalise, strip combining marks, lowercase, non-alphanumeric → `-`, collapse repeats, strip leading/trailing `-`, truncate to 60.
- Collision suffix: `-2`, `-3`, … based on existing directories.
- `lock()`: `fcntl.flock` on `<project>/.lock` (exclusive, blocking) inside a `@contextmanager`. On Linux this is sufficient; do not build a cross-platform abstraction.
- `save()` must call `model_dump_json(indent=2)` so `project.json` stays diffable by hand.

- [ ] **Step 3: Run tests, then commit**

```bash
git add src/videomaker/project.py tests/unit/test_project.py
git commit -s -m "feat: ProjectStore with atomic writes, slugs and per-project lock"
```

---

### Task 3: Hash engine and caches

**Files:**
- Create: `src/videomaker/cache.py`
- Test: `tests/unit/test_cache.py`

**Interfaces:**
- Produces:
  - `hash_inputs(**parts) -> str` — stable SHA-256 over a canonical JSON dump, returned as 16 hex chars. Key order must not affect the result; floats are rounded to 6 dp before hashing.
  - `StageCache(path: Path)` — persists `{stage_key: input_hash}` to `cache/stages.json`. Methods: `is_stale(key, current_hash) -> bool`, `mark(key, current_hash) -> None`, `invalidate(prefix: str) -> None`, `save() -> None`.
  - `ResponseCache(root: Path, ttl_days: int | None = None)` — content-addressed blob cache under `~/.cache/ai-video-maker/`. Methods: `get(key: str) -> dict | None`, `put(key: str, value: dict) -> None`. `ttl_days=None` means never expire (LLM); the stock cache passes `7`.
  - Constant `STAGE_ORDER: tuple[str, ...] = ("script", "voice", "align", "visuals", "captions", "assemble", "render")`.
  - `stage_key(stage: str, unit: str = "all") -> str` → `f"{stage}:{unit}"`.
- Consumed by Tasks 8–15.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_cache.py`:

```python
import time

from videomaker.cache import ResponseCache, StageCache, hash_inputs, stage_key


def test_hash_is_order_independent():
    assert hash_inputs(a=1, b="x") == hash_inputs(b="x", a=1)


def test_hash_changes_with_any_input():
    base = hash_inputs(text="hello", voice="af_heart", speed=1.0)
    assert base != hash_inputs(text="hello!", voice="af_heart", speed=1.0)
    assert base != hash_inputs(text="hello", voice="af_bella", speed=1.0)
    assert base != hash_inputs(text="hello", voice="af_heart", speed=1.1)


def test_hash_tolerates_float_noise():
    assert hash_inputs(d=1.0000000001) == hash_inputs(d=1.0)


def test_hash_is_stable_across_runs():
    # Guards against dict/set iteration order or repr() leaking in.
    assert hash_inputs(text="hello", n=3) == hash_inputs(text="hello", n=3)


def test_stage_cache_reports_missing_as_stale(tmp_path):
    cache = StageCache(tmp_path / "stages.json")
    assert cache.is_stale(stage_key("voice", "s01"), "abc") is True


def test_stage_cache_marks_and_persists(tmp_path):
    path = tmp_path / "stages.json"
    cache = StageCache(path)
    cache.mark(stage_key("voice", "s01"), "abc")
    cache.save()

    reloaded = StageCache(path)
    assert reloaded.is_stale(stage_key("voice", "s01"), "abc") is False
    assert reloaded.is_stale(stage_key("voice", "s01"), "different") is True


def test_invalidate_by_prefix_is_scoped(tmp_path):
    cache = StageCache(tmp_path / "stages.json")
    cache.mark(stage_key("voice", "s01"), "a")
    cache.mark(stage_key("voice", "s02"), "b")
    cache.mark(stage_key("align", "s01"), "c")
    cache.invalidate("voice:")
    assert cache.is_stale(stage_key("voice", "s01"), "a") is True
    assert cache.is_stale(stage_key("voice", "s02"), "b") is True
    assert cache.is_stale(stage_key("align", "s01"), "c") is False


def test_response_cache_round_trips(tmp_path):
    cache = ResponseCache(tmp_path)
    assert cache.get("k1") is None
    cache.put("k1", {"scenes": [1, 2]})
    assert cache.get("k1") == {"scenes": [1, 2]}


def test_response_cache_expires_with_ttl(tmp_path, monkeypatch):
    cache = ResponseCache(tmp_path, ttl_days=7)
    cache.put("k1", {"v": 1})
    assert cache.get("k1") == {"v": 1}
    monkeypatch.setattr(time, "time", lambda: time.time() + 8 * 86400)
    assert cache.get("k1") is None


def test_response_cache_survives_corrupt_entry(tmp_path):
    cache = ResponseCache(tmp_path)
    cache.put("k1", {"v": 1})
    # A truncated write must degrade to a miss, never crash the pipeline.
    for blob in tmp_path.rglob("*.json"):
        blob.write_text("{not json")
    assert cache.get("k1") is None
```

- [ ] **Step 2: Run tests to verify they fail**, then implement.

Implementation notes for `hash_inputs`:

```python
def _canonical(value):
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    return value


def hash_inputs(**parts: object) -> str:
    blob = json.dumps(_canonical(parts), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]
```

`ResponseCache` stores each entry as `<root>/<key[:2]>/<key>.json` containing `{"stored_at": <epoch>, "value": {...}}`. `get()` must catch `json.JSONDecodeError` and `OSError` and return `None` — a corrupt cache is a miss, never an error.

- [ ] **Step 3: Run tests, then commit**

```bash
git add src/videomaker/cache.py tests/unit/test_cache.py
git commit -s -m "feat: content-hash stage cache and TTL response cache"
```

---

### Task 4: Niche templates

**Files:**
- Create: `src/videomaker/templates.py`, `templates/_schema.md`, `templates/tech_explainer.yaml`
- Test: `tests/unit/test_templates.py`

**Interfaces:**
- Produces: `Template` (pydantic) with fields `name: str`, `display_name: str`, `system_prompt: str`, `structure: list[str]` (named beats), `words_per_minute: int = 150`, `scene_count: tuple[int, int]`, `visual_kind_order: list[VisualKind]`, `caption_style: str = "default"`, `music_mood: str = "calm"`; and `load_template(name: str, templates_dir: Path | None = None) -> Template`, `list_templates(templates_dir=None) -> list[str]`.
- `Template.target_scene_count(minutes: float) -> int` clamps `minutes * wpm / avg_words_per_scene` into `scene_count`.
- Consumed by Task 12 (script stage).

Only `tech_explainer` is required in M1; M4 adds the other three plus `templates lint`.

- [ ] **Step 1: Write the failing tests**

```python
import pytest

from videomaker.models import VisualKind
from videomaker.templates import Template, list_templates, load_template


def test_tech_explainer_loads():
    t = load_template("tech_explainer")
    assert t.name == "tech_explainer"
    assert t.system_prompt.strip()
    assert len(t.structure) >= 3
    assert t.visual_kind_order[0] in set(VisualKind)


def test_unknown_template_raises_with_available_names():
    with pytest.raises(ValueError) as exc:
        load_template("no_such_template")
    assert "tech_explainer" in str(exc.value)


def test_list_templates_includes_shipped_ones():
    assert "tech_explainer" in list_templates()


@pytest.mark.parametrize("minutes,expected_range", [(0.5, (3, 12)), (2.0, (3, 12)), (10.0, (3, 12))])
def test_target_scene_count_stays_in_bounds(minutes, expected_range):
    t = load_template("tech_explainer")
    n = t.target_scene_count(minutes)
    assert expected_range[0] <= n <= expected_range[1]


def test_invalid_template_yaml_is_rejected(tmp_path):
    (tmp_path / "broken.yaml").write_text("display_name: no name field\n")
    with pytest.raises(ValueError):
        load_template("broken", templates_dir=tmp_path)
```

- [ ] **Step 2: Run tests to verify they fail**, then implement.

`templates/tech_explainer.yaml` should set a system prompt that produces **original explanatory prose**, a `structure` of hook → context → mechanism → implication → close, `scene_count: [4, 10]`, and `visual_kind_order: [stock_video, stock_photo, ai_image]` (stock first keeps neuron spend near zero).

`templates/_schema.md` documents every field for M4 contributors — this is the file that makes templates a no-Python contribution path.

- [ ] **Step 3: Run tests, then commit**

```bash
git add src/videomaker/templates.py templates/ tests/unit/test_templates.py
git commit -s -m "feat: YAML niche templates with schema validation; tech_explainer"
```

---

### Task 5: Provider ABCs, registry and error taxonomy

**Files:**
- Create: `src/videomaker/providers/__init__.py`, `src/videomaker/providers/base.py`, `src/videomaker/providers/errors.py`
- Modify: `src/videomaker/config.py` (add provider chain settings)
- Test: `tests/unit/test_provider_registry.py`

**Interfaces:**
- Produces (errors): `ProviderError` (base), `QuotaExceeded`, `TransientError(retry_after_s: float | None)`, `ProviderConfigError`, `ProviderResponseError`.
- Produces (result types): `LLMResult(text, model, cached: bool)`, `TTSResult(path, duration_s, sample_rate)`.
- Produces (ABCs), all sync:
  - `LLMProvider.generate(*, system: str, user: str, json_schema: dict | None = None, temperature: float = 0.7, max_tokens: int = 2048) -> LLMResult`
  - `TTSProvider.voices() -> list[str]` / `synthesize(*, text, voice, out_path: Path, speed: float = 1.0, language: str = "en") -> TTSResult`
  - `STTProvider.transcribe_words(*, audio_path: Path, language: str = "en", hint_text: str = "") -> list[WordTiming]`
  - `ImageProvider.generate_image(*, prompt: str, out_path: Path, aspect: Aspect, negative_prompt: str = "", seed: int | None = None) -> AssetRef`
  - `StockProvider.search(*, query, kind: VisualKind, min_duration_s: float = 0.0, orientation: str = "landscape", per_page: int = 4) -> list[StockResult]` / `download(result: StockResult, out_path: Path, *, max_height: int = 1080) -> AssetRef`
  - `Uploader.upload(...)` — declared in M1, implemented in M5.
- Produces (registry): `@register(kind: str, name: str)` decorator, `get_provider(kind, name, settings) -> object`, `resolve_chain(kind, settings) -> list[str]`.
- Config additions to `Settings`: `provider_chains: dict[str, list[str]]` defaulting to `{"llm": ["groq", "gemini"], "tts": ["kokoro"], "stt": ["fasterwhisper"], "stock": ["pexels"], "image": ["cloudflare"]}`, loadable from `config.yaml` under a `providers:` key.

- [ ] **Step 1: Write the failing tests**

```python
import pytest

from videomaker.config import Settings, load_settings
from videomaker.providers import get_provider, register, resolve_chain
from videomaker.providers.errors import ProviderConfigError


def test_register_and_retrieve():
    @register("llm", "dummy_for_test")
    class Dummy:
        def __init__(self, settings):
            self.settings = settings

    got = get_provider("llm", "dummy_for_test", Settings())
    assert isinstance(got, Dummy)


def test_unknown_provider_raises_config_error_listing_known_names():
    with pytest.raises(ProviderConfigError) as exc:
        get_provider("llm", "nope", Settings())
    assert "nope" in str(exc.value)


def test_default_chains():
    s = Settings()
    assert resolve_chain("llm", s) == ["groq", "gemini"]
    assert resolve_chain("stock", s) == ["pexels"]


def test_config_yaml_overrides_chain(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("providers:\n  llm: [gemini, groq]\n")
    s = load_settings(cfg)
    assert resolve_chain("llm", s) == ["gemini", "groq"]


def test_registering_same_name_twice_raises():
    @register("llm", "dupe_test")
    class A:
        def __init__(self, settings): ...

    with pytest.raises(ProviderConfigError):

        @register("llm", "dupe_test")
        class B:
            def __init__(self, settings): ...
```

- [ ] **Step 2: Run tests to verify they fail**, then implement.

The registry is a module-level `dict[tuple[str, str], type]`. `providers/__init__.py` imports every concrete provider module at the bottom of the file so decorators run on import — but wrap those imports so a missing optional dependency degrades to "provider unavailable", not an ImportError at CLI startup.

- [ ] **Step 3: Run tests, then commit**

```bash
git add src/videomaker/providers/ src/videomaker/config.py tests/unit/test_provider_registry.py
git commit -s -m "feat: provider ABCs, decorator registry, error taxonomy, chain config"
```

---

### Task 6: Rate limiting and quota budgets

**Files:**
- Create: `src/videomaker/providers/ratelimit.py`
- Test: `tests/unit/test_ratelimit.py`

**Interfaces:**
- Produces: `Budget(rpm: int | None, per_day: int | None, per_hour: int | None)`; `QuotaTracker(path: Path, clock: Callable[[], float] = time.time)` with `check(provider: str, budget: Budget) -> None` (raises `QuotaExceeded`), `record(provider: str, units: int = 1) -> None`, `remaining(provider, budget) -> dict[str, int | None]`, `save()`.
- Constant `SOFT_BUDGETS: dict[str, Budget]` — deliberately **under** the published hard limits: `groq` 28 rpm, `gemini` 240/day, `pexels` 190/hour, `cloudflare` 9000 neurons/day.
- Persisted to `~/.cache/ai-video-maker/quota.json`.

**Why neurons, not requests, for Cloudflare:** M0 finding 6 — Workers AI silently bills past the free cap. `record("cloudflare", units=58)` per 1024² Flux image; the tracker stops us before the cap rather than after.

- [ ] **Step 1: Write the failing tests**

```python
import pytest

from videomaker.providers.errors import QuotaExceeded
from videomaker.providers.ratelimit import Budget, QuotaTracker


class FakeClock:
    def __init__(self): self.now = 1_000_000.0
    def __call__(self): return self.now
    def advance(self, seconds): self.now += seconds


def test_allows_calls_under_rpm(tmp_path):
    clock = FakeClock()
    q = QuotaTracker(tmp_path / "quota.json", clock=clock)
    budget = Budget(rpm=3, per_day=None, per_hour=None)
    for _ in range(3):
        q.check("groq", budget)
        q.record("groq")


def test_blocks_when_rpm_exceeded(tmp_path):
    clock = FakeClock()
    q = QuotaTracker(tmp_path / "quota.json", clock=clock)
    budget = Budget(rpm=2, per_day=None, per_hour=None)
    for _ in range(2):
        q.check("groq", budget); q.record("groq")
    with pytest.raises(QuotaExceeded):
        q.check("groq", budget)


def test_rpm_window_slides(tmp_path):
    clock = FakeClock()
    q = QuotaTracker(tmp_path / "quota.json", clock=clock)
    budget = Budget(rpm=2, per_day=None, per_hour=None)
    for _ in range(2):
        q.check("groq", budget); q.record("groq")
    clock.advance(61)
    q.check("groq", budget)  # window rolled over


def test_daily_budget_counts_units_not_calls(tmp_path):
    clock = FakeClock()
    q = QuotaTracker(tmp_path / "quota.json", clock=clock)
    budget = Budget(rpm=None, per_day=100, per_hour=None)
    q.check("cloudflare", budget); q.record("cloudflare", units=58)
    q.check("cloudflare", budget); q.record("cloudflare", units=58)
    with pytest.raises(QuotaExceeded):
        q.check("cloudflare", budget)


def test_counters_persist_across_instances(tmp_path):
    path = tmp_path / "quota.json"
    clock = FakeClock()
    budget = Budget(rpm=None, per_day=2, per_hour=None)
    q1 = QuotaTracker(path, clock=clock)
    q1.check("groq", budget); q1.record("groq"); q1.save()
    q2 = QuotaTracker(path, clock=clock)
    q2.check("groq", budget); q2.record("groq"); q2.save()
    q3 = QuotaTracker(path, clock=clock)
    with pytest.raises(QuotaExceeded):
        q3.check("groq", budget)


def test_remaining_reports_headroom(tmp_path):
    q = QuotaTracker(tmp_path / "quota.json", clock=FakeClock())
    budget = Budget(rpm=10, per_day=None, per_hour=None)
    q.record("groq", units=3)
    assert q.remaining("groq", budget)["rpm"] == 7
```

- [ ] **Step 2: Run tests to verify they fail**, then implement, then commit.

```bash
git commit -s -m "feat: quota tracker with sliding-window rpm and persisted daily budgets"
```

---

### Task 7: Mock providers

**Files:**
- Create: `src/videomaker/providers/mock.py`, `tests/fixtures/{sample_photo.jpg,sample_clip.mp4}`
- Test: `tests/unit/test_mock_providers.py`

**Interfaces:**
- Produces `MockLLM`, `MockTTS`, `MockSTT`, `MockImage`, `MockStock`, registered under name `"mock"` for each kind.
- `MockTTS.synthesize` writes a **real** wav (silence at 24 kHz mono, duration derived deterministically from word count: `0.4s per word`) so ffprobe and FFmpeg treat it as genuine audio.
- `MockSTT.transcribe_words` returns evenly-spaced timings covering the clip, derived from `hint_text` — so snap-to-script has something realistic to align against.
- `MockStock.download` and `MockImage.generate_image` copy the checked-in fixtures.
- Fixtures are generated once by FFmpeg (`testsrc2`/`color`) and committed — keep both **under 100 KB**.

**This task is load-bearing:** the golden-path test and `--providers mock` both depend on these being faithful enough that the real FFmpeg pipeline exercises the same code paths.

- [ ] **Step 1: Generate the fixtures**

```bash
mkdir -p tests/fixtures
ffmpeg -y -f lavfi -i "color=c=slategray:s=1920x1080:d=1" -frames:v 1 tests/fixtures/sample_photo.jpg
ffmpeg -y -f lavfi -i "testsrc2=s=1920x1080:r=30:d=8" -c:v libx264 -preset veryfast -crf 34 -pix_fmt yuv420p -an tests/fixtures/sample_clip.mp4
ls -lh tests/fixtures/
```

Confirm both are under 100 KB; raise `-crf` if the clip is larger.

- [ ] **Step 2: Write tests asserting the mocks satisfy the ABCs**

Assert: every mock is an instance of its ABC; `MockTTS` writes a file ffprobe reports with the expected duration (±0.05 s); `MockSTT` returns monotonically non-decreasing timings whose last `end_s` does not exceed the audio duration; `MockStock.search` returns exactly `per_page` results; `MockImage.generate_image` produces a readable JPEG with the requested aspect's dimensions.

- [ ] **Step 3: Implement, run, commit**

```bash
git commit -s -m "feat: mock providers and test fixtures for offline pipeline runs"
```

---

### Task 8: LLM providers (Groq + Gemini)

**Files:**
- Create: `src/videomaker/providers/llm/__init__.py`, `groq.py`, `gemini.py`
- Test: `tests/unit/test_llm_providers.py`

**Interfaces:**
- Consumes: `LLMProvider` ABC, `ResponseCache`, `QuotaTracker`, `Settings`.
- Produces: `GroqProvider`, `GeminiProvider` registered as `llm/groq`, `llm/gemini`.
- Both accept an injected `client: httpx.Client | None` for testing (mirrors M0's `download_file`).
- Module constant `GROQ_MODEL_PREFERENCE: tuple[str, ...] = ("openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b", "groq/compound")`.

**M0 findings 1 and 2 are the whole point of this task.** `GroqProvider._resolve_model()` fetches `/openai/v1/models`, intersects with `GROQ_MODEL_PREFERENCE` in order, and **raises `ProviderConfigError` naming both the preference list and what the account actually offers** when nothing matches. It must never pick an arbitrary id.

- [ ] **Step 1: Write the failing tests**

```python
import httpx
import pytest

from videomaker.config import Settings
from videomaker.providers.errors import ProviderConfigError, QuotaExceeded, TransientError
from videomaker.providers.llm.groq import GROQ_MODEL_PREFERENCE, GroqProvider

MODELS_BODY = {"data": [{"id": "allam-2-7b"}, {"id": "openai/gpt-oss-120b"}, {"id": "whisper-large-v3"}]}
ONLY_JUNK = {"data": [{"id": "allam-2-7b"}, {"id": "whisper-large-v3"}]}


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_resolves_preferred_model_not_first_listed():
    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=MODELS_BODY)
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    p = GroqProvider(Settings(groq_api_key="k"), client=_client(handler))
    assert p._resolve_model() == "openai/gpt-oss-120b"


def test_raises_when_no_preferred_model_available():
    def handler(request):
        return httpx.Response(200, json=ONLY_JUNK)

    p = GroqProvider(Settings(groq_api_key="k"), client=_client(handler))
    with pytest.raises(ProviderConfigError) as exc:
        p._resolve_model()
    message = str(exc.value)
    assert "allam-2-7b" in message                      # what the account has
    assert GROQ_MODEL_PREFERENCE[0] in message          # what we wanted
    assert "allam-2-7b" not in (p.__dict__.get("model") or "")  # never silently adopted


def test_generate_returns_text_and_marks_uncached():
    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=MODELS_BODY)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"scenes": []}'}}]})

    p = GroqProvider(Settings(groq_api_key="k"), client=_client(handler))
    result = p.generate(system="s", user="u")
    assert result.text == '{"scenes": []}'
    assert result.cached is False
    assert result.model == "openai/gpt-oss-120b"


def test_429_maps_to_transient_with_retry_after():
    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=MODELS_BODY)
        return httpx.Response(429, headers={"Retry-After": "12"}, json={})

    p = GroqProvider(Settings(groq_api_key="k"), client=_client(handler))
    with pytest.raises(TransientError) as exc:
        p.generate(system="s", user="u")
    assert exc.value.retry_after_s == 12


def test_missing_key_fails_fast_as_config_error():
    p = GroqProvider(Settings(groq_api_key=""), client=_client(lambda r: httpx.Response(200)))
    with pytest.raises(ProviderConfigError):
        p.generate(system="s", user="u")


def test_daily_quota_exhaustion_maps_to_quota_exceeded():
    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=MODELS_BODY)
        return httpx.Response(429, json={"error": {"code": "daily_limit_exceeded"}})

    p = GroqProvider(Settings(groq_api_key="k"), client=_client(handler))
    with pytest.raises(QuotaExceeded):
        p.generate(system="s", user="u")
```

Write the mirror-image tests for `GeminiProvider` against `generativelanguage.googleapis.com`, including its `x-goog-api-key` header and its different error envelope.

- [ ] **Step 2: Run tests to verify they fail**, then implement.

Notes:
- Cache key: `hash_inputs(provider=..., model=..., system=..., user=..., temperature=..., schema=...)`. A cache hit returns `LLMResult(cached=True)` and **must not** touch the quota tracker.
- `_resolve_model()` result is memoised per instance; the model list call itself is cached for the process lifetime.
- Distinguish 429-rate-limit (→ `TransientError` with `Retry-After`) from 429-daily-cap (→ `QuotaExceeded`) by inspecting the error body, since only the latter should advance the fallback chain.
- Groq supports `response_format={"type": "json_object"}`; Gemini uses `generationConfig.responseMimeType`. Both are set when `json_schema` is passed.

- [ ] **Step 3: Run tests, then commit**

```bash
git commit -s -m "feat: groq and gemini LLM providers with runtime model resolution"
```

---

### Task 9: TTS and STT providers

**Files:**
- Create: `src/videomaker/providers/tts/{__init__.py,kokoro_onnx.py}`, `src/videomaker/providers/stt/{__init__.py,fasterwhisper.py}`, `src/videomaker/align.py`
- Test: `tests/unit/test_snap_to_script.py`, `tests/unit/test_tts_stt_contracts.py`

**Interfaces:**
- Produces: `KokoroTTS` (`tts/kokoro`), `FasterWhisperSTT` (`stt/fasterwhisper`), and the pure function `snap_to_script(script_text: str, heard: list[WordTiming]) -> list[WordTiming]`.
- `KokoroTTS` lazily imports `kokoro_onnx` and holds the `Kokoro` instance as an **instance attribute created once**; likewise `FasterWhisperSTT` constructs `WhisperModel` once (**M0 finding 2**).
- `KokoroTTS.synthesize` writes with an explicit `subtype="PCM_16"` and returns the true duration (**M0 finding 4**).
- `FasterWhisperSTT.transcribe_words` passes `initial_prompt=hint_text` and honours an optional `HF_TOKEN` (**M0 finding 9**).

**snap-to-script is the subtle part.** Whisper produces its own spelling ("Kakoro" for "Kokoro" — measured in M0); we want the *script's* words carrying *whisper's* timings. Use `difflib.SequenceMatcher` over normalised token lists:
- `equal` blocks: copy timings straight across.
- `replace` blocks: distribute the heard block's total span proportionally across the script tokens by character length.
- `delete` (script word never heard): give it a zero-width slice at the previous word's end.
- `insert` (heard word not in script): drop it.
- The result must have exactly one entry per script token, in order, monotonically non-decreasing.

- [ ] **Step 1: Write the failing tests**

```python
from videomaker.align import snap_to_script
from videomaker.models import WordTiming


def w(word, start, end): return WordTiming(word=word, start_s=start, end_s=end)


def test_exact_match_passes_timings_through():
    heard = [w("hello", 0.0, 0.5), w("world", 0.5, 1.0)]
    out = snap_to_script("hello world", heard)
    assert [x.word for x in out] == ["hello", "world"]
    assert out[1].start_s == 0.5


def test_misheard_word_keeps_script_spelling():
    heard = [w("this", 0.0, 0.2), w("is", 0.2, 0.3), w("Kakoro", 0.3, 0.9)]
    out = snap_to_script("This is Kokoro", heard)
    assert [x.word for x in out] == ["This", "is", "Kokoro"]
    assert out[2].start_s == 0.3 and out[2].end_s == 0.9


def test_punctuation_and_case_do_not_break_alignment():
    heard = [w("hello", 0.0, 0.4), w("world", 0.4, 0.9)]
    out = snap_to_script("Hello, world!", heard)
    assert [x.word for x in out] == ["Hello,", "world!"]


def test_script_word_never_heard_gets_zero_width_slot():
    heard = [w("alpha", 0.0, 0.4), w("gamma", 0.4, 0.8)]
    out = snap_to_script("alpha beta gamma", heard)
    assert [x.word for x in out] == ["alpha", "beta", "gamma"]
    assert out[1].start_s == out[1].end_s == 0.4


def test_extra_heard_word_is_dropped():
    heard = [w("alpha", 0.0, 0.3), w("um", 0.3, 0.4), w("beta", 0.4, 0.8)]
    out = snap_to_script("alpha beta", heard)
    assert [x.word for x in out] == ["alpha", "beta"]


def test_many_to_one_replacement_splits_span_proportionally():
    heard = [w("gigabyte", 0.0, 1.0)]
    out = snap_to_script("giga byte", heard)
    assert len(out) == 2
    assert out[0].start_s == 0.0 and out[1].end_s == 1.0
    assert out[0].end_s == out[1].start_s


def test_output_is_always_monotonic():
    heard = [w("a", 0.0, 0.5), w("b", 0.5, 0.6), w("c", 0.6, 2.0)]
    out = snap_to_script("x y z w", heard)
    for prev, nxt in zip(out, out[1:]):
        assert prev.end_s <= nxt.start_s + 1e-9


def test_empty_transcription_still_returns_one_slot_per_word():
    out = snap_to_script("alpha beta", [])
    assert [x.word for x in out] == ["alpha", "beta"]
```

- [ ] **Step 2: Run tests to verify they fail**, then implement `align.py` and both providers.

The TTS/STT contract tests should be marked `@pytest.mark.slow` and skipped when the `ml` extra is absent, so CI (which installs without `ml`) stays green:

```python
ml = pytest.importorskip("kokoro_onnx")
```

- [ ] **Step 3: Run tests, then commit**

```bash
git commit -s -m "feat: kokoro TTS and faster-whisper STT providers with snap-to-script"
```

---

### Task 10: Stock and image providers

**Files:**
- Create: `src/videomaker/providers/stock/{__init__.py,pexels.py}`, `src/videomaker/providers/image/{__init__.py,cloudflare.py}`
- Test: `tests/unit/test_stock_image_providers.py`

**Interfaces:**
- Produces: `PexelsProvider` (`stock/pexels`), `CloudflareImageProvider` (`image/cloudflare`).
- Both take an injected `client: httpx.Client | None`.
- `PexelsProvider.search` filters per the spec: videos must be **≥ scene duration + 0.5 s gap** and **≥ 1080p**; photos must have **min side ≥ 1600** so both crops work later. Results are cached via `ResponseCache(ttl_days=7)`; a cache hit must not consume the hourly budget.
- `PexelsProvider.download` streams to a `.part` file then `os.replace` — reuse M0's `download_file` rather than reimplementing it.
- `CloudflareImageProvider` pins `CLOUDFLARE_IMAGE_MODEL = "@cf/black-forest-labs/flux-1-schnell"` and `FLUX_STEPS = 4`, records `NEURONS_PER_IMAGE = 58` against the quota tracker (**M0 finding 6**), and appends composition guidance to the prompt (**M0 finding 5**) so the 1024² square survives a 16:9 crop — e.g. *"centred subject, generous headroom and margins, no text"*.
- `AssetRef.attribution` must be populated for Pexels (`photographer`/`user.name`) — M5's attribution block depends on it and the Pexels terms require it.

- [ ] **Step 1: Write the failing tests**

Cover, with `httpx.MockTransport`: a video shorter than the scene is rejected; a sub-1080p video is rejected; photos below 1600px min-side are rejected; `per_page` results are returned in rank order; attribution is captured; a second identical search hits the cache and issues **zero** HTTP requests; a 429 becomes `TransientError`; Flux success writes a real JPEG and records 58 neurons; Flux 401/403 becomes `ProviderConfigError`; exhausted neuron budget raises `QuotaExceeded` **before** the HTTP call.

- [ ] **Step 2: Run tests to verify they fail**, then implement, then commit.

```bash
git commit -s -m "feat: pexels stock and cloudflare flux image providers with budgets"
```

---

### Task 11: FFmpeg runner and ASS caption writer

**Files:**
- Modify: `src/videomaker/media/ffmpeg.py` (extend M0's probe module)
- Create: `src/videomaker/media/ass.py`
- Test: `tests/unit/test_ffmpeg_runner.py`, `tests/unit/test_ass_writer.py`

**Interfaces:**
- `media/ffmpeg.py` gains: `run_ffmpeg(args: list[str], *, cwd: Path | None = None, on_progress: Callable[[float], None] | None = None) -> None` (raises `FFmpegError` with the **tail of stderr**, parses `-progress pipe:1` `out_time_ms=` lines); `probe_json(path: Path) -> dict`; `probe_duration(path: Path) -> float`; `probe_dimensions(path: Path) -> tuple[int, int]`.
- `media/ass.py` gains: `CaptionStyle` dataclass (`font_size`, `words_per_chunk`, `margin_v`, `alignment`, `primary_colour`, `outline_colour`, `outline`, `shadow`); `STYLES: dict[Aspect, CaptionStyle]` — **wide only in M1** (`font_size=64`, `words_per_chunk=5`, lower-third); `chunk_words(words, per_chunk) -> list[list[WordTiming]]`; `write_ass(words, style, out_path, *, play_res: tuple[int, int]) -> Path`; `ass_colour(r, g, b) -> str`.

**Two traps, both unit-tested:**
- **ASS colours are BGR, not RGB.** `ass_colour(255, 0, 0)` (red) must produce `&H000000FF`. Assert this directly.
- **Zero-gap timings (M0 finding 3).** `chunk_words` must inset each chunk's end by 40 ms (never past the next chunk's start, never below a 200 ms minimum duration) or captions collide edge-to-edge.

- [ ] **Step 1: Write the failing tests**

```python
from videomaker.media.ass import STYLES, ass_colour, chunk_words, write_ass
from videomaker.models import Aspect, WordTiming


def w(word, s, e): return WordTiming(word=word, start_s=s, end_s=e)


def test_ass_colour_is_bgr_not_rgb():
    assert ass_colour(255, 0, 0) == "&H000000FF"   # red
    assert ass_colour(0, 0, 255) == "&H00FF0000"   # blue
    assert ass_colour(255, 255, 255) == "&H00FFFFFF"


def test_chunks_respect_size():
    words = [w(str(i), i * 0.3, (i + 1) * 0.3) for i in range(11)]
    chunks = chunk_words(words, 5)
    assert [len(c) for c in chunks] == [5, 5, 1]


def test_chunks_are_inset_so_they_do_not_touch():
    words = [w(str(i), i * 0.5, (i + 1) * 0.5) for i in range(4)]  # zero-gap, as whisper emits
    chunks = chunk_words(words, 2)
    first_end = chunks[0][-1].end_s
    second_start = chunks[1][0].start_s
    assert first_end < second_start


def test_write_ass_emits_valid_header_and_events(tmp_path):
    words = [w("hello", 0.0, 0.5), w("world", 0.5, 1.0)]
    out = write_ass(words, STYLES[Aspect.WIDE], tmp_path / "c.ass", play_res=(1920, 1080))
    text = out.read_text()
    assert "[Script Info]" in text and "PlayResX: 1920" in text
    assert "[V4+ Styles]" in text and "[Events]" in text
    assert text.count("Dialogue:") == 1          # both words fit one chunk
    assert "hello world" in text


def test_timestamps_are_ass_formatted(tmp_path):
    words = [w("x", 3661.5, 3662.0)]  # 1:01:01.50
    out = write_ass(words, STYLES[Aspect.WIDE], tmp_path / "c.ass", play_res=(1920, 1080))
    assert "1:01:01.50" in out.read_text()


def test_braces_in_narration_are_escaped(tmp_path):
    # Unescaped { } would be parsed as ASS override tags and silently vanish.
    out = write_ass([w("{drop}", 0.0, 1.0)], STYLES[Aspect.WIDE], tmp_path / "c.ass",
                    play_res=(1920, 1080))
    assert "\\{drop\\}" in out.read_text() or "(drop)" in out.read_text()


def test_empty_word_list_writes_header_only(tmp_path):
    out = write_ass([], STYLES[Aspect.WIDE], tmp_path / "c.ass", play_res=(1920, 1080))
    assert "Dialogue:" not in out.read_text()
```

For `run_ffmpeg`, test with real trivial FFmpeg invocations (`-f lavfi -i color=...`): success, failure raising `FFmpegError` containing stderr, and `on_progress` receiving increasing values.

- [ ] **Step 2: Run tests to verify they fail**, then implement, then commit.

```bash
git commit -s -m "feat: ffmpeg runner with progress parsing and ASS caption writer"
```

---

### Task 12: Pipeline stages — script, voice, align

**Files:**
- Create: `src/videomaker/pipeline/__init__.py`, `base.py`, `script.py`, `voice.py`, `align.py`
- Test: `tests/unit/test_stage_script.py`, `tests/unit/test_stage_voice_align.py`

**Interfaces:**
- `pipeline/base.py`: `StageDeps` dataclass carrying `settings`, `store`, `stage_cache`, `response_cache`, `quota`, and a `provider(kind) -> object` callable that walks the configured chain and advances on `QuotaExceeded`/`ProviderConfigError`. Also `StageResult(changed: bool, skipped_units: int)`.
- `script.py`: `run_script(project, deps) -> StageResult` — unit `all`. Builds the prompt from the template, calls the LLM with a JSON schema, pydantic-validates into scenes, **one repair retry** feeding the validation error back, then the next provider in the chain. Assigns stable ids `s01…`. Hash inputs: `topic`, `template name + version`, `target_minutes`, `language`, `model`.
- `voice.py`: `run_voice(project, deps) -> StageResult` — unit per scene. Hash: `narration`, `voice`, `speed`, `provider`. Writes `scenes/sNN/narration.wav`, sets `Scene.audio_path` (relative) and `Scene.duration_s` via `probe_duration`.
- `align.py`: `run_align(project, deps) -> StageResult` — unit per scene. Hash: `narration`, `audio hash`, `stt provider`. Calls `transcribe_words(hint_text=narration)` then `snap_to_script`, writes `scenes/sNN/words.json`, sets `Scene.words`.

**The cache-scoping test is the important one** — it is the DoD's "<10 s re-run" made concrete, and it is the single most valuable regression test in M1:

```python
def test_editing_one_scene_invalidates_only_that_scene(tmp_path, mock_deps):
    project = _voiced_project_with_three_scenes(tmp_path, mock_deps)
    calls_before = mock_deps.tts_calls
    project.scene_by_id("s02").narration = "Completely different narration now."
    run_voice(project, mock_deps)
    assert mock_deps.tts_calls == calls_before + 1        # only s02 re-synthesised
    assert project.scene_by_id("s01").audio_path is not None
    assert project.scene_by_id("s03").audio_path is not None
```

Also test: a clean re-run makes **zero** provider calls; a malformed LLM response triggers exactly one repair retry then succeeds; a persistently malformed response advances to the next provider; a scene marked `locked=True` is never re-run even when stale.

- [ ] **Step 1: Write the failing tests, using the mock providers from Task 7.**
- [ ] **Step 2: Run to verify they fail, implement, run to verify they pass.**
- [ ] **Step 3: Commit**

```bash
git commit -s -m "feat: script, voice and align pipeline stages with per-scene caching"
```

---

### Task 13: Pipeline stages — visuals, captions

**Files:**
- Create: `src/videomaker/pipeline/visuals.py`, `src/videomaker/pipeline/captions.py`
- Test: `tests/unit/test_stage_visuals.py`, `tests/unit/test_stage_captions.py`

**Interfaces:**
- `visuals.py`: `run_visuals(project, deps) -> StageResult` — unit per scene. Resolves `VisualKind.AUTO` through the template's `visual_kind_order`, searching stock first and falling back to AI image only when stock returns nothing usable (keeps neuron spend near zero). Stores up to 4 `candidates` and sets `chosen` to the first. Downloads to `scenes/sNN/asset.<ext>`. Hash: `query`, `kind`, `duration_s`, `provider`.
- `captions.py`: `run_captions(project, deps) -> StageResult` — unit per **aspect** (`captions:wide`). Concatenates every scene's words with each scene's cumulative time offset, then calls `write_ass`. Hash: all scene word lists + style + aspect.

The offset arithmetic deserves an explicit test: scene 2's first word must start at exactly `scene 1 duration + inter-scene gap`, not at its within-scene time.

- [ ] **Step 1–3: TDD as above, then commit**

```bash
git commit -s -m "feat: visuals and captions pipeline stages"
```

---

### Task 14: Pipeline stages — assemble and render (wide)

**Files:**
- Create: `src/videomaker/pipeline/assemble.py`, `src/videomaker/pipeline/render.py`
- Test: `tests/unit/test_stage_assemble.py`, `tests/integration/test_render_wide.py`

**Interfaces:**
- `assemble.py`: `build_scene_filter(scene, spec, *, gap_s) -> str` (pure, unit-tested as a **string**), `run_assemble(project, deps) -> StageResult`. Produces one uniform silent intermediate per scene in `build/` (libx264, `crf 18`, `veryfast`, 30 fps, exact duration = scene audio + gap), writes `build/concat_wide.txt` and `build/timeline_wide.json`, then concatenates with the **concat demuxer using stream copy**.
- `render.py`: `run_render(project, deps) -> StageResult`. Single final pass: burns `subtitles=captions/wide.ass:fontsdir=assets/fonts`, muxes the concatenated narration, applies `loudnorm I=-14:TP=-1.5`, `-movflags +faststart`, writes `output/final_wide.mp4` and sets `OutputSpec.video_path`.

**Critical detail carried from the M0 spike:** run FFmpeg with `cwd=<project dir>` and **relative paths** for the subtitles filter. An absolute path containing a colon or backslash breaks the filter-graph parser; the M0 spike hit exactly this and fixed it by setting `cwd`. Do not pass an absolute `.ass` path.

**Stills** default to `Motion.PAN` — an animated crop over a 120 % pre-scale, which is smoother and cheaper than `zoompan`. `Motion.ZOOM` (Ken Burns) is opt-in and must pre-upscale 2× to avoid the jitter the M0 spike showed.

The pure filter-graph tests keep this fast:

```python
def test_still_pan_filter_prescales_before_cropping():
    graph = build_scene_filter(_still_scene(duration=4.0), WIDE_SPEC, gap_s=0.3)
    assert "scale=" in graph
    assert graph.index("scale=") < graph.index("crop=")   # never crop before scaling


def test_video_scene_is_trimmed_and_looped_to_exact_duration():
    graph = build_scene_filter(_video_scene(duration=4.0, asset_duration=2.0), WIDE_SPEC, gap_s=0.3)
    assert "loop" in graph or "stream_loop" in graph
```

`tests/integration/test_render_wide.py` runs the **real** FFmpeg over mock-provider artefacts and asserts with ffprobe: 1920×1080, 30 fps, h264 + aac, duration within 0.3 s of the sum of scene durations plus gaps, and a non-zero-size file.

- [ ] **Step 1–3: TDD as above, then commit**

```bash
git commit -s -m "feat: assemble and render stages producing final_wide.mp4"
```

---

### Task 15: CLI and stage orchestration

**Files:**
- Create: `src/videomaker/runner.py`
- Modify: `src/videomaker/cli.py`
- Test: `tests/unit/test_runner.py`, `tests/unit/test_cli_m1.py`

**Interfaces:**
- `runner.py`: `derive_status(project, stage_cache) -> Status` (walks `STAGE_ORDER`, returns at the first stale/missing unit or unapproved gate); `run_pipeline(project, deps, *, until: str | None = None, yes: bool = False, on_stage: Callable | None = None) -> Project`.
- CLI commands: `new TOPIC -t/--template -m/--minutes --voice`; `run PROJECT_ID [--yes] [--until STAGE] [--providers mock]`; `status PROJECT_ID`; `list`.
- Gates: without `--yes`, `run` stops at the first unapproved gate and prints what to review. With `--yes`, gates auto-approve and stamp `Approvals`.
- Exit codes: `0` success, `1` stage failure, `2` blocked at a gate.

Status derivation deserves direct tests: a project whose `s02` narration changed after voicing reports `SCRIPT_READY`, not `VOICED`; approving a gate then editing an earlier stage clears the downstream approval.

- [ ] **Step 1–3: TDD as above, then commit**

```bash
git commit -s -m "feat: pipeline runner, derived status, and new/run/status/list commands"
```

---

### Task 16: Golden-path integration test and CI

**Files:**
- Create: `tests/integration/test_golden_path.py`, `tests/conftest.py`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Produces: the end-to-end regression that guards the M1 DoD forever.

The test, using `--providers mock` and **real FFmpeg**:

1. `new "how ssds work" -t tech_explainer` → project on disk.
2. `run <id> --yes` → `output/final_wide.mp4` exists; ffprobe says 1920×1080, h264+aac, sane duration.
3. Count provider calls. Re-run `run <id> --yes` → **zero** provider calls, **zero** FFmpeg re-encodes, and it completes in under 10 s (assert on a monotonic clock, generously — CI runners are slow).
4. Edit `s02`'s narration, re-run → exactly one TTS call, one STT call, one re-encode of that scene, and a fresh final render.

CI changes: install FFmpeg explicitly (`sudo apt-get update && sudo apt-get install -y ffmpeg`) rather than relying on the runner image, keep `uv sync` **without** the `ml` extra, and run unit + integration tests. Contract tests against real providers stay skipped without keys.

- [ ] **Step 1: Write the test and watch it fail for the right reason**, then make it pass.
- [ ] **Step 2: Verify CI is green on a pushed branch before merging.**
- [ ] **Step 3: Commit**

```bash
git commit -s -m "test: golden-path integration test with mock providers and real ffmpeg"
```

---

### Task 17: M1 definition-of-done verification

**Files:**
- Create: `docs/superpowers/spike-results-m1.md`

- [ ] **Step 1: Run the real DoD battery** (real providers, not mocks)

```bash
uv run videomaker new "how ssds work" -t tech_explainer
uv run videomaker run <id> --yes            # expect: playable output/final_wide.mp4
time uv run videomaker run <id> --yes       # expect: < 10s, no provider calls
uv run videomaker status <id>               # expect: rendered
uv run pytest -q && uv run ruff check .
uv run videomaker doctor                    # expect: still all green
```

Watch the MP4. Confirm: captions are readable and in sync, visuals change per scene, narration matches the script, no black frames at scene boundaries.

- [ ] **Step 2: Record results** in `docs/superpowers/spike-results-m1.md`: wall time for a cold run and a warm run, quota actually consumed (Groq calls, Pexels requests, Cloudflare neurons), the resolved Groq model, per-stage timings, and every follow-up discovered for M2/M3.

- [ ] **Step 3: Commit and push**

```bash
git commit -s -m "docs: M1 complete — CLI happy path renders final_wide.mp4"
```

---

## Self-review notes

- **Spec coverage (M1 scope)**: ProjectStore ✓ (T2), hash engine ✓ (T3), providers + mocks ✓ (T5–T10), stages through render ✓ (T12–T14), golden-path CI test ✓ (T16). Deliberately out of scope per the spec: web UI (M2), vertical/karaoke/music/thumbnail/`clean` (M3), remaining templates + lint (M4), metadata + upload (M5), Docker (M6).
- **M0 findings honoured**: runtime Groq model resolution + loud failure (T8), single `WhisperModel` (T9), caption inset for zero-gap timings (T11), explicit wav subtype (T9), Flux composition headroom + pinned model + neuron budget (T6, T10), per-scene narration timing (T12), relative subtitle paths under `cwd` (T14).
- **Interface consistency**: `StageDeps` is constructed once in `runner.py` and threaded through every stage; `AssetRef.local_path` is relative everywhere so project folders stay movable; `Aspect`-keyed `outputs` means M3 adds vertical without touching M1's signatures.
- **Riskiest task is T14** (FFmpeg filter graphs). It is deliberately split into pure string-builder unit tests plus one real-render integration test, so failures are diagnosable without reading FFmpeg stderr.
- **Sequencing**: T1→T2→T3 are strictly ordered; T4–T7 are independent of each other and can run in parallel; T8–T10 depend on T5/T6 and are mutually independent; T11 is independent of T8–T10; T12→T13→T14 are strictly ordered; T15 needs all stages; T16 needs T15.
