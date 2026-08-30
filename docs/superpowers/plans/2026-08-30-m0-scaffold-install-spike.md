# M0 — Scaffold + Install Spike Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A working project skeleton whose `videomaker doctor` and `videomaker setup` commands verify/prepare everything the video pipeline will need on the primary dev machine — an HP ProBook 450 G10 running Zorin OS 18 (Linux x86_64) — proving the ML stacks (kokoro-onnx/onnxruntime, faster-whisper) install and run, and that FFmpeg has libass, before any feature work.

**Architecture:** `src/` layout Python package `videomaker` managed by `uv` (Python 3.12 pinned). Typer CLI with `version`, `doctor`, `setup` commands. Doctor checks are pure functions returning `CheckResult` values rendered by the CLI; setup downloads Kokoro/Whisper models to `~/.cache/ai-video-maker/models` and runs TTS+STT smoke tests (the spike). ML deps live in an optional `ml` extra so the scaffold installs even if an ML wheel fails to resolve.

**Tech Stack:** uv, Typer, pydantic + pydantic-settings, PyYAML, httpx, rich, pytest, ruff, GitHub Actions. ML extra: kokoro-onnx, onnxruntime, soundfile, faster-whisper.

## Global Constraints

- **$0 cost**: no paid services, no paid dependencies, GitHub Actions free tier only.
- **Python `>=3.12,<3.13`** pinned via `.python-version` — never the system Python.
- **Primary platform: Linux x86_64** (Zorin OS 18, Ubuntu-based) on HP ProBook 450 G10 (i7-1355U, 12 threads, 16GB RAM). All ML packages ship manylinux wheels, so installs are expected to be clean. macOS (incl. Intel) stays supported for future contributors — its quirks are documented in `docs/macos-intel-notes.md` (created in Task 9), not handled by special-case code.
- **No PyTorch dependency**: TTS is kokoro-onnx (lighter, CPU-friendly, keeps Intel-Mac contributors viable). No local image gen (no discrete GPU). Heavy ML deps go in the `ml` extra, imported lazily inside functions.
- **License AGPL-3.0-only**; package name `videomaker`, distribution name `ai-video-maker`.
- **No MoviePy anywhere.** FFmpeg/ffprobe via subprocess only.
- Sign off every commit (`git commit -s`, DCO).
- Working directory for all commands: the repo root (the clone of the private GitHub repo on the HP).
- Spike findings must be recorded in `docs/superpowers/spike-results-m0.md` — the M1 plan depends on them.
- **HP bootstrap prerequisites** (once, before Task 1): `sudo apt update && sudo apt install -y git curl ffmpeg`, install uv (`curl -LsSf https://astral.sh/uv/install.sh | sh`), clone the repo, and install Claude Code to run this plan.

---

### Task 1: Project scaffold + CLI stub

**Files:**
- Create: `pyproject.toml`, `.python-version`, `.gitignore`, `src/videomaker/__init__.py`, `src/videomaker/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces: Typer app `videomaker.cli:app`, entry point `videomaker.cli:main`, `videomaker.__version__: str`. Later tasks add commands to this `app` object.

- [x] **Step 1: Write project metadata files**

`pyproject.toml`:

```toml
[project]
name = "ai-video-maker"
version = "0.0.1"
description = "Local-first, human-in-the-loop AI video studio"
readme = "README.md"
license = "AGPL-3.0-only"
requires-python = ">=3.12,<3.13"
dependencies = [
    "typer>=0.12",
    "pydantic>=2.7",
    "pydantic-settings>=2.3",
    "pyyaml>=6.0",
    "httpx>=0.27",
    "rich>=13.7",
]

[project.optional-dependencies]
ml = [
    "kokoro-onnx>=0.4",
    "soundfile>=0.12",
    "faster-whisper>=1.0",
]

[dependency-groups]
dev = [
    "pytest>=8.2",
    "ruff>=0.5",
]

[project.scripts]
videomaker = "videomaker.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/videomaker"]

[tool.ruff]
line-length = 100
src = ["src", "tests"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

`.python-version`:

```
3.12
```

`.gitignore`:

```
.venv/
__pycache__/
*.pyc
.pytest_cache/
.ruff_cache/
workspace/
.env
config.yaml
*.part
.DS_Store
```

- [x] **Step 2: Write the failing CLI test**

`tests/test_cli.py`:

```python
from typer.testing import CliRunner

from videomaker import __version__
from videomaker.cli import app

runner = CliRunner()


def test_version_command_prints_version():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.output
```

- [x] **Step 3: Run test to verify it fails**

Run: `uv sync && uv run pytest tests/test_cli.py -v`
Expected: FAIL (ModuleNotFoundError: videomaker) — `uv sync` itself may error until the package files exist; that error counts as the failing state.

- [x] **Step 4: Implement the package + CLI stub**

`src/videomaker/__init__.py`:

```python
__version__ = "0.0.1"
```

`src/videomaker/cli.py`:

```python
import typer

from videomaker import __version__

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.callback()
def _root() -> None:
    """AI Video Maker — local-first, human-in-the-loop video studio."""


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)


def main() -> None:
    app()
```

- [x] **Step 5: Run test to verify it passes**

Run: `uv sync && uv run pytest tests/test_cli.py -v`
Expected: PASS (1 passed). Also run `uv run videomaker version` → prints `0.0.1`, and `uv run ruff check .` → no errors.

- [x] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock .python-version .gitignore src/ tests/
git commit -s -m "feat: project scaffold with uv, Typer CLI stub, pytest + ruff"
```

---

### Task 2: Settings + config loading

**Files:**
- Create: `src/videomaker/config.py`, `config.example.yaml`, `.env.example`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Settings` (pydantic BaseSettings) with fields `workspace_dir: Path`, `models_dir: Path`, `music_dir: Path`, `groq_api_key: str`, `gemini_api_key: str`, `pexels_api_key: str`, `cloudflare_account_id: str`, `cloudflare_api_token: str`; and `load_settings(config_file: Path | None = None) -> Settings`. Consumed by Tasks 4, 6, 7, 8.

- [x] **Step 1: Write the failing tests**

`tests/test_config.py`:

```python
from pathlib import Path

from videomaker.config import Settings, load_settings


def test_defaults_when_no_config_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = load_settings()
    assert settings.workspace_dir == Path("workspace")
    assert settings.models_dir == Path.home() / ".cache" / "ai-video-maker" / "models"


def test_config_yaml_overrides_paths(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("paths:\n  workspace_dir: /tmp/ws\n  models_dir: /tmp/models\n")
    settings = load_settings(cfg)
    assert settings.workspace_dir == Path("/tmp/ws")
    assert settings.models_dir == Path("/tmp/models")


def test_api_key_read_from_plain_env_var(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gk-test")
    assert Settings().groq_api_key == "gk-test"
```

- [x] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL (ModuleNotFoundError: videomaker.config)

- [x] **Step 3: Implement config.py**

`src/videomaker/config.py`:

```python
from pathlib import Path

import yaml
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_CONFIG_FILE = Path("config.yaml")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    workspace_dir: Path = Path("workspace")
    models_dir: Path = Path.home() / ".cache" / "ai-video-maker" / "models"
    music_dir: Path = Path("assets/music")

    groq_api_key: str = Field(default="", validation_alias=AliasChoices("GROQ_API_KEY"))
    gemini_api_key: str = Field(default="", validation_alias=AliasChoices("GEMINI_API_KEY"))
    pexels_api_key: str = Field(default="", validation_alias=AliasChoices("PEXELS_API_KEY"))
    cloudflare_account_id: str = Field(
        default="", validation_alias=AliasChoices("CLOUDFLARE_ACCOUNT_ID")
    )
    cloudflare_api_token: str = Field(
        default="", validation_alias=AliasChoices("CLOUDFLARE_API_TOKEN")
    )


def load_settings(config_file: Path | None = None) -> Settings:
    path = config_file or DEFAULT_CONFIG_FILE
    overrides: dict[str, Path] = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text()) or {}
        for key, value in (raw.get("paths") or {}).items():
            if key in {"workspace_dir", "models_dir", "music_dir"}:
                overrides[key] = Path(str(value)).expanduser()
    return Settings(**overrides)
```

- [x] **Step 4: Write the example config files**

`config.example.yaml`:

```yaml
# Copy to config.yaml and adjust. Secrets go in .env, never here.
paths:
  workspace_dir: ./workspace
  models_dir: ~/.cache/ai-video-maker/models
  music_dir: ./assets/music
```

`.env.example`:

```
# Copy to .env and fill in. All of these have permanent free tiers.
# Groq (script generation, ~30 req/min free): https://console.groq.com -> API Keys
GROQ_API_KEY=
# Google AI Studio (script generation fallback, free): https://aistudio.google.com/apikey
GEMINI_API_KEY=
# Pexels (stock footage, 200 req/hour free): https://www.pexels.com/api/
PEXELS_API_KEY=
# Cloudflare Workers AI (AI images, ~10k neurons/day free): https://dash.cloudflare.com -> AI -> Workers AI
CLOUDFLARE_ACCOUNT_ID=
CLOUDFLARE_API_TOKEN=
```

- [x] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS (3 passed)

- [x] **Step 6: Commit**

```bash
git add src/videomaker/config.py config.example.yaml .env.example tests/test_config.py
git commit -s -m "feat: settings with config.yaml paths and plain-env API keys"
```

---

### Task 3: FFmpeg capability probe

**Files:**
- Create: `src/videomaker/media/__init__.py` (empty), `src/videomaker/media/ffmpeg.py`
- Test: `tests/test_ffmpeg_probe.py`

**Interfaces:**
- Produces: `FFmpegCaps` frozen dataclass (`installed: bool`, `version: str`, `has_subtitles_filter: bool`, `hw_encoder: str`, `has_ffprobe: bool`) and `probe_capabilities() -> FFmpegCaps`. `hw_encoder` is the first available of `h264_qsv`, `h264_vaapi`, `h264_videotoolbox`, else `""` — platform-neutral (QSV/VA-API on the HP's Intel graphics, VideoToolbox on Macs). Consumed by Task 4's doctor checks. This module later grows the render helpers in M1.

- [x] **Step 1: Write the failing tests**

`tests/test_ffmpeg_probe.py`:

```python
from videomaker.media import ffmpeg as ff

FILTERS_WITH_SUBTITLES = """Filters:
 T.. subtitles         V->V       Render text subtitles onto input video using the libass library.
 ... scale             V->V       Scale the input video size.
"""

FILTERS_WITHOUT_SUBTITLES = """Filters:
 ... scale             V->V       Scale the input video size.
"""

ENCODERS_WITH_QSV = """Encoders:
 V..... h264_qsv             H.264 (Intel Quick Sync Video acceleration)
 V..... h264_vaapi           H.264 (VAAPI)
"""


def test_probe_when_ffmpeg_missing(monkeypatch):
    monkeypatch.setattr(ff.shutil, "which", lambda name: None)
    caps = ff.probe_capabilities()
    assert caps.installed is False
    assert caps.has_subtitles_filter is False


def test_probe_detects_subtitles_and_hw_encoder(monkeypatch):
    monkeypatch.setattr(ff.shutil, "which", lambda name: "/usr/bin/" + name)
    outputs = {
        "-version": "ffmpeg version 6.1.1 Copyright\n",
        "-filters": FILTERS_WITH_SUBTITLES,
        "-encoders": ENCODERS_WITH_QSV,
    }
    monkeypatch.setattr(ff, "_run", lambda args: outputs[args[-1]])
    caps = ff.probe_capabilities()
    assert caps.installed is True
    assert caps.version.startswith("ffmpeg version 6.1.1")
    assert caps.has_subtitles_filter is True
    assert caps.hw_encoder == "h264_qsv"


def test_probe_detects_missing_libass_and_no_hw_encoder(monkeypatch):
    monkeypatch.setattr(ff.shutil, "which", lambda name: "/usr/bin/" + name)
    outputs = {
        "-version": "ffmpeg version 6.1.1\n",
        "-filters": FILTERS_WITHOUT_SUBTITLES,
        "-encoders": "",
    }
    monkeypatch.setattr(ff, "_run", lambda args: outputs[args[-1]])
    caps = ff.probe_capabilities()
    assert caps.has_subtitles_filter is False
    assert caps.hw_encoder == ""
```

- [x] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ffmpeg_probe.py -v`
Expected: FAIL (ModuleNotFoundError: videomaker.media)

- [x] **Step 3: Implement the probe**

`src/videomaker/media/__init__.py`: empty file.

`src/videomaker/media/ffmpeg.py`:

```python
import shutil
import subprocess
from dataclasses import dataclass


HW_ENCODER_PREFERENCE = ("h264_qsv", "h264_vaapi", "h264_videotoolbox")


@dataclass(frozen=True)
class FFmpegCaps:
    installed: bool
    version: str
    has_subtitles_filter: bool
    hw_encoder: str  # first available of HW_ENCODER_PREFERENCE, else ""
    has_ffprobe: bool


def _run(args: list[str]) -> str:
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    return result.stdout


def probe_capabilities() -> FFmpegCaps:
    if shutil.which("ffmpeg") is None:
        return FFmpegCaps(False, "", False, "", False)
    version_out = _run(["ffmpeg", "-hide_banner", "-version"])
    version = version_out.splitlines()[0].strip() if version_out else "unknown"
    filters = _run(["ffmpeg", "-hide_banner", "-filters"])
    encoders = _run(["ffmpeg", "-hide_banner", "-encoders"])
    hw_encoder = next((name for name in HW_ENCODER_PREFERENCE if name in encoders), "")
    return FFmpegCaps(
        installed=True,
        version=version,
        has_subtitles_filter=" subtitles " in filters,
        hw_encoder=hw_encoder,
        has_ffprobe=shutil.which("ffprobe") is not None,
    )
```

- [x] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ffmpeg_probe.py -v`
Expected: PASS (3 passed)

- [x] **Step 5: Commit**

```bash
git add src/videomaker/media/ tests/test_ffmpeg_probe.py
git commit -s -m "feat: ffmpeg capability probe (subtitles filter, hw encoders)"
```

---

### Task 4: `videomaker doctor` command

**Files:**
- Create: `src/videomaker/doctor.py`
- Modify: `src/videomaker/cli.py` (add `doctor` command)
- Test: `tests/test_doctor.py`

**Interfaces:**
- Consumes: `Settings`/`load_settings` (Task 2), `FFmpegCaps`/`probe_capabilities` (Task 3).
- Produces: `CheckResult` frozen dataclass (`name: str`, `level: str` — one of `"ok" | "warn" | "fail"` — `detail: str`, `fix: str = ""`), `run_checks(settings: Settings, caps: FFmpegCaps) -> list[CheckResult]`, and constant `FFMPEG_LIBASS_FIX: str`. Task 6 extends `MODEL_FILES`.

- [x] **Step 1: Write the failing tests**

`tests/test_doctor.py`:

```python
from pathlib import Path

from videomaker.config import Settings
from videomaker.doctor import FFMPEG_LIBASS_FIX, run_checks
from videomaker.media.ffmpeg import FFmpegCaps

GOOD_CAPS = FFmpegCaps(True, "ffmpeg version 6.1.1", True, "h264_qsv", True)
NO_LIBASS_CAPS = FFmpegCaps(True, "ffmpeg version 6.1.1", False, "h264_qsv", True)


def _by_name(results, name):
    return next(r for r in results if r.name == name)


def test_missing_subtitles_filter_fails_with_remediation(tmp_path):
    settings = Settings(workspace_dir=tmp_path, models_dir=tmp_path / "models")
    results = run_checks(settings, NO_LIBASS_CAPS)
    check = _by_name(results, "ffmpeg subtitles filter")
    assert check.level == "fail"
    assert check.fix == FFMPEG_LIBASS_FIX
    assert "apt install ffmpeg" in check.fix


def test_good_ffmpeg_passes(tmp_path):
    settings = Settings(workspace_dir=tmp_path, models_dir=tmp_path / "models")
    results = run_checks(settings, GOOD_CAPS)
    assert _by_name(results, "ffmpeg subtitles filter").level == "ok"
    assert _by_name(results, "ffmpeg").level == "ok"


def test_missing_models_warns_with_setup_fix(tmp_path):
    settings = Settings(workspace_dir=tmp_path, models_dir=tmp_path / "models")
    results = run_checks(settings, GOOD_CAPS)
    check = _by_name(results, "kokoro model files")
    assert check.level == "warn"
    assert "videomaker setup" in check.fix


def test_present_models_pass(tmp_path):
    models = tmp_path / "models"
    models.mkdir(parents=True)
    (models / "kokoro-v1.0.onnx").write_bytes(b"x")
    (models / "voices-v1.0.bin").write_bytes(b"x")
    settings = Settings(workspace_dir=tmp_path, models_dir=models)
    results = run_checks(settings, GOOD_CAPS)
    assert _by_name(results, "kokoro model files").level == "ok"


def test_no_llm_key_warns(tmp_path, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    settings = Settings(
        workspace_dir=tmp_path, models_dir=tmp_path / "models",
        groq_api_key="", gemini_api_key="",
    )
    results = run_checks(settings, GOOD_CAPS)
    assert _by_name(results, "LLM API key").level == "warn"
```

- [x] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_doctor.py -v`
Expected: FAIL (ModuleNotFoundError: videomaker.doctor)

- [x] **Step 3: Implement doctor.py**

`src/videomaker/doctor.py`:

```python
import shutil
import sys
from dataclasses import dataclass

from videomaker.config import Settings
from videomaker.media.ffmpeg import FFmpegCaps

FFMPEG_LIBASS_FIX = (
    "install an FFmpeg build with libass — Debian/Ubuntu: 'sudo apt install ffmpeg'; "
    "macOS: 'brew reinstall ffmpeg' (standard bottles include the 'subtitles' filter)"
)
SETUP_FIX = "uv run videomaker setup"
MODEL_FILES = ("kokoro-v1.0.onnx", "voices-v1.0.bin")
MIN_FREE_GB = 20


@dataclass(frozen=True)
class CheckResult:
    name: str
    level: str  # "ok" | "warn" | "fail"
    detail: str
    fix: str = ""


def _check_python() -> CheckResult:
    v = sys.version_info
    if (v.major, v.minor) == (3, 12):
        return CheckResult("python", "ok", f"{v.major}.{v.minor}.{v.micro}")
    return CheckResult(
        "python", "fail", f"running {v.major}.{v.minor}.{v.micro}, need 3.12.x",
        fix="uv sync   # respects .python-version",
    )


def _check_ffmpeg(caps: FFmpegCaps) -> list[CheckResult]:
    if not caps.installed:
        return [
            CheckResult("ffmpeg", "fail", "not found on PATH",
                        fix="sudo apt install ffmpeg   # (brew install ffmpeg on macOS)")
        ]
    results = [CheckResult("ffmpeg", "ok", caps.version)]
    if caps.has_subtitles_filter:
        results.append(CheckResult("ffmpeg subtitles filter", "ok", "libass available"))
    else:
        results.append(
            CheckResult(
                "ffmpeg subtitles filter", "fail",
                "this ffmpeg build lacks libass — captions cannot be burned",
                fix=FFMPEG_LIBASS_FIX,
            )
        )
    if not caps.has_ffprobe:
        results.append(
            CheckResult("ffprobe", "fail", "not found on PATH",
                        fix="sudo apt install ffmpeg   # (brew install ffmpeg on macOS)")
        )
    results.append(
        CheckResult(
            "hardware encoder", "ok" if caps.hw_encoder else "warn",
            f"{caps.hw_encoder} available for fast renders" if caps.hw_encoder
            else "none detected; libx264 (CPU) only",
        )
    )
    return results


def _check_disk(settings: Settings) -> CheckResult:
    target = settings.workspace_dir if settings.workspace_dir.exists() else settings.workspace_dir.parent
    if not target.exists():
        target = target.parent
    free_gb = shutil.disk_usage(target).free / 1e9
    if free_gb >= MIN_FREE_GB:
        return CheckResult("disk space", "ok", f"{free_gb:.0f} GB free")
    return CheckResult(
        "disk space", "warn", f"only {free_gb:.0f} GB free (want >= {MIN_FREE_GB} GB)",
        fix="uv run videomaker clean --keep-outputs   # (available from M3)",
    )


def _check_models(settings: Settings) -> CheckResult:
    missing = [f for f in MODEL_FILES if not (settings.models_dir / f).exists()]
    if not missing:
        return CheckResult("kokoro model files", "ok", str(settings.models_dir))
    return CheckResult(
        "kokoro model files", "warn", f"missing: {', '.join(missing)}", fix=SETUP_FIX
    )


def _check_api_keys(settings: Settings) -> list[CheckResult]:
    results = []
    if settings.groq_api_key or settings.gemini_api_key:
        results.append(CheckResult("LLM API key", "ok", "groq and/or gemini configured"))
    else:
        results.append(
            CheckResult(
                "LLM API key", "warn", "neither GROQ_API_KEY nor GEMINI_API_KEY set",
                fix="cp .env.example .env and fill in at least one (both are free)",
            )
        )
    if settings.pexels_api_key:
        results.append(CheckResult("Pexels API key", "ok", "configured"))
    else:
        results.append(
            CheckResult("Pexels API key", "warn", "PEXELS_API_KEY not set",
                        fix="free key at https://www.pexels.com/api/")
        )
    return results


def run_checks(settings: Settings, caps: FFmpegCaps) -> list[CheckResult]:
    results = [_check_python()]
    results.extend(_check_ffmpeg(caps))
    results.append(_check_disk(settings))
    results.append(_check_models(settings))
    results.extend(_check_api_keys(settings))
    return results
```

- [x] **Step 4: Add the doctor command to the CLI**

Modify `src/videomaker/cli.py` — replace the whole file with:

```python
import typer
from rich.console import Console
from rich.table import Table

from videomaker import __version__

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()

LEVEL_STYLE = {"ok": "green", "warn": "yellow", "fail": "red"}


@app.callback()
def _root() -> None:
    """AI Video Maker — local-first, human-in-the-loop video studio."""


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)


@app.command()
def doctor() -> None:
    """Check that this machine can run the video pipeline."""
    from videomaker.config import load_settings
    from videomaker.doctor import run_checks
    from videomaker.media.ffmpeg import probe_capabilities

    results = run_checks(load_settings(), probe_capabilities())
    table = Table(title="videomaker doctor")
    table.add_column("check")
    table.add_column("status")
    table.add_column("detail")
    table.add_column("fix")
    for r in results:
        style = LEVEL_STYLE[r.level]
        table.add_row(r.name, f"[{style}]{r.level.upper()}[/{style}]", r.detail, r.fix)
    console.print(table)
    if any(r.level == "fail" for r in results):
        raise typer.Exit(code=1)


def main() -> None:
    app()
```

- [x] **Step 5: Run tests to verify they pass**

Run: `uv run pytest -v`
Expected: PASS (all tests). Then run `uv run videomaker doctor` on the HP.
Expected: table renders; ffmpeg + subtitles filter OK (apt builds include libass), models WARN, API keys WARN. Task 5 confirms the FFmpeg environment and starts the spike log.

- [x] **Step 6: Commit**

```bash
git add src/videomaker/doctor.py src/videomaker/cli.py tests/test_doctor.py
git commit -s -m "feat: doctor command with ffmpeg/disk/model/key checks"
```

---

### Task 5: Verify the FFmpeg environment on the HP + start spike log

**Files:**
- Create: `docs/superpowers/spike-results-m0.md`

**Interfaces:**
- Produces: a verified FFmpeg-with-libass environment on the HP; the spike-results file that Tasks 6–8 append to and the M1 plan reads.

- [x] **Step 1: Verify FFmpeg is installed with libass**

Run: `ffmpeg -hide_banner -filters | grep -E "subtitles|drawtext"`
Expected: lines for `subtitles` and `drawtext` (Ubuntu/Zorin apt builds include libass). If ffmpeg is missing entirely: `sudo apt update && sudo apt install -y ffmpeg`, then re-check.

- [x] **Step 2: Check for a hardware encoder (optional, warn-level)**

Run: `ffmpeg -hide_banner -encoders | grep -E "h264_qsv|h264_vaapi"`
Expected: at least `h264_vaapi` (the i7-1355U's Intel graphics support Quick Sync). If neither appears and you want hardware fast-renders later: `sudo apt install -y intel-media-va-driver-non-free vainfo` and re-check. Not a blocker — libx264 on 12 threads is the default anyway.

- [x] **Step 3: Verify doctor is green on ffmpeg**

Run: `uv run videomaker doctor`
Expected: `ffmpeg`, `ffmpeg subtitles filter`, `ffprobe` all OK; `hardware encoder` OK or WARN. Remaining WARNs (models, API keys) are expected at this point.

- [x] **Step 4: Record the result**

`docs/superpowers/spike-results-m0.md`:

```markdown
# M0 Spike Results

Machine: HP ProBook 450 G10 — i7-1355U (12 threads), 16GB RAM, Zorin OS 18.1
(Ubuntu-based), Python 3.12 via uv.

## FFmpeg environment (Task 5)
- Version: <paste `ffmpeg -version | head -1` output>
- subtitles/drawtext filters: <present / installed via apt>
- Hardware encoder: <h264_qsv | h264_vaapi | none>

## kokoro-onnx / onnxruntime wheels (Task 6)
- (pending)

## Kokoro TTS smoke test (Task 7)
- (pending)

## faster-whisper / ctranslate2 wheels (Task 8)
- (pending)
```

Fill in the actual values before committing.

- [x] **Step 5: Commit**

```bash
git add docs/superpowers/spike-results-m0.md
git commit -s -m "chore: verify ffmpeg/libass environment on HP; start M0 spike log"
```

---

### Task 6: Model downloader + `videomaker setup` (the onnxruntime wheel spike)

**Files:**
- Create: `src/videomaker/downloads.py`, `src/videomaker/setup_cmd.py`
- Modify: `src/videomaker/cli.py` (add `setup` command)
- Test: `tests/test_downloads.py`, `tests/test_setup_cmd.py`

**Interfaces:**
- Consumes: `Settings`/`load_settings` (Task 2).
- Produces: `download_file(url: str, dest: Path, *, client: httpx.Client | None = None) -> Path`; `ensure_models(settings: Settings, downloader=download_file) -> list[Path]`; constants `KOKORO_MODEL_URL`, `KOKORO_VOICES_URL`. Tasks 7–8 add `spike_tts`/`spike_stt` to `setup_cmd.py`.

- [x] **Step 1: Install the ml extra**

Run: `uv sync --extra ml`

Expected: clean install — on Linux x86_64 every package (onnxruntime, kokoro-onnx, ctranslate2, faster-whisper, soundfile) ships current manylinux wheels. Record the resolved versions (`uv pip list | grep -iE "onnx|kokoro|whisper|ctranslate"`) in `docs/superpowers/spike-results-m0.md`. In the unlikely event something fails to resolve or crashes on import, record it and apply the pin ladder documented in `docs/macos-intel-notes.md` (Task 9) — it applies analogously on any platform.

- [x] **Step 2: Write the failing download-helper test**

`tests/test_downloads.py`:

```python
import httpx

from videomaker.downloads import download_file


def test_download_file_streams_to_dest(tmp_path):
    def handler(request):
        return httpx.Response(200, content=b"model-bytes")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    dest = tmp_path / "sub" / "model.onnx"
    result = download_file("https://example.com/model.onnx", dest, client=client)
    assert result == dest
    assert dest.read_bytes() == b"model-bytes"
    assert not dest.with_suffix(".onnx.part").exists()


def test_download_file_raises_on_http_error(tmp_path):
    def handler(request):
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    dest = tmp_path / "model.onnx"
    try:
        download_file("https://example.com/missing", dest, client=client)
        raise AssertionError("expected HTTPStatusError")
    except httpx.HTTPStatusError:
        assert not dest.exists()
```

- [x] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_downloads.py -v`
Expected: FAIL (ModuleNotFoundError: videomaker.downloads)

- [x] **Step 4: Implement downloads.py**

`src/videomaker/downloads.py`:

```python
from pathlib import Path

import httpx


def download_file(url: str, dest: Path, *, client: httpx.Client | None = None) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    own_client = client is None
    if own_client:
        client = httpx.Client(follow_redirects=True, timeout=120.0)
    try:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            with open(tmp, "wb") as fh:
                for chunk in response.iter_bytes(256 * 1024):
                    fh.write(chunk)
        tmp.replace(dest)
        return dest
    finally:
        tmp.unlink(missing_ok=True)
        if own_client:
            client.close()
```

- [x] **Step 5: Write the failing setup-orchestration test**

`tests/test_setup_cmd.py`:

```python
from videomaker.config import Settings
from videomaker.setup_cmd import KOKORO_MODEL_URL, KOKORO_VOICES_URL, ensure_models


def test_ensure_models_downloads_missing_files(tmp_path):
    calls = []

    def fake_downloader(url, dest, **kwargs):
        calls.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x")
        return dest

    settings = Settings(workspace_dir=tmp_path, models_dir=tmp_path / "models")
    paths = ensure_models(settings, downloader=fake_downloader)
    assert sorted(calls) == sorted([KOKORO_MODEL_URL, KOKORO_VOICES_URL])
    assert all(p.exists() for p in paths)


def test_ensure_models_skips_existing_files(tmp_path):
    models = tmp_path / "models"
    models.mkdir(parents=True)
    (models / "kokoro-v1.0.onnx").write_bytes(b"x")
    (models / "voices-v1.0.bin").write_bytes(b"x")
    calls = []

    def fake_downloader(url, dest, **kwargs):
        calls.append(url)
        return dest

    settings = Settings(workspace_dir=tmp_path, models_dir=models)
    ensure_models(settings, downloader=fake_downloader)
    assert calls == []
```

- [x] **Step 6: Run tests to verify they fail, then implement setup_cmd.py**

Run: `uv run pytest tests/test_setup_cmd.py -v` → FAIL (ModuleNotFoundError).

`src/videomaker/setup_cmd.py`:

```python
from pathlib import Path

from videomaker.config import Settings
from videomaker.downloads import download_file

_RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
KOKORO_MODEL_URL = f"{_RELEASE}/kokoro-v1.0.onnx"
KOKORO_VOICES_URL = f"{_RELEASE}/voices-v1.0.bin"

MODEL_TARGETS = {
    KOKORO_MODEL_URL: "kokoro-v1.0.onnx",   # ~310 MB
    KOKORO_VOICES_URL: "voices-v1.0.bin",   # ~27 MB
}


def ensure_models(settings: Settings, downloader=download_file) -> list[Path]:
    paths = []
    for url, filename in MODEL_TARGETS.items():
        dest = settings.models_dir / filename
        if not dest.exists():
            downloader(url, dest)
        paths.append(dest)
    return paths
```

Add to `src/videomaker/cli.py` (below the `doctor` command, same import style):

```python
@app.command()
def setup() -> None:
    """Download models and run TTS/STT smoke tests (first-run setup)."""
    from videomaker.config import load_settings
    from videomaker.setup_cmd import ensure_models

    settings = load_settings()
    settings.workspace_dir.mkdir(parents=True, exist_ok=True)
    settings.models_dir.mkdir(parents=True, exist_ok=True)
    console.print("Downloading models (first run only; ~340 MB)...")
    for path in ensure_models(settings):
        console.print(f"  [green]ready[/green] {path}")
    console.print("Setup complete. Run [bold]videomaker doctor[/bold] to verify.")
```

Also verify the import now: `uv run python -c "from kokoro_onnx import Kokoro; print('kokoro-onnx import OK')"`. Record the working versions in the spike log.

- [x] **Step 7: Run tests to verify they pass**

Run: `uv run pytest -v`
Expected: PASS (all)

- [x] **Step 8: Download the real models on the HP**

Run: `uv run videomaker setup`
Expected: both files download to `~/.cache/ai-video-maker/models/` (~340 MB total; a few minutes). Then `uv run videomaker doctor` → `kokoro model files` = OK.

- [x] **Step 9: Update spike log and commit**

Fill in the "kokoro-onnx / onnxruntime wheels" section of `docs/superpowers/spike-results-m0.md` with the exact package versions installed and any pins that were needed (expected: none).

```bash
git add src/videomaker/downloads.py src/videomaker/setup_cmd.py src/videomaker/cli.py \
        tests/test_downloads.py tests/test_setup_cmd.py pyproject.toml uv.lock \
        docs/superpowers/spike-results-m0.md
git commit -s -m "feat: setup command downloads kokoro models; ml extra verified on linux"
```

---

### Task 7: Kokoro TTS smoke test in setup

**Files:**
- Modify: `src/videomaker/setup_cmd.py` (add `spike_tts`), `src/videomaker/cli.py` (call it from `setup`)
- Test: manual on this Mac (real model inference is not unit-testable; CI never runs it)

**Interfaces:**
- Consumes: `ensure_models`, `Settings`.
- Produces: `spike_tts(settings: Settings) -> tuple[Path, float, float]` — (wav path, audio seconds, wall-clock seconds). M1's Kokoro TTS provider is written against the same `kokoro_onnx` API calls proven here.

- [x] **Step 1: Implement spike_tts**

Append to `src/videomaker/setup_cmd.py`:

```python
import time


def spike_tts(settings: Settings) -> tuple[Path, float, float]:
    """Synthesize a short line with Kokoro; returns (wav_path, audio_s, wall_s)."""
    import soundfile as sf
    from kokoro_onnx import Kokoro

    kokoro = Kokoro(
        str(settings.models_dir / "kokoro-v1.0.onnx"),
        str(settings.models_dir / "voices-v1.0.bin"),
    )
    started = time.monotonic()
    samples, sample_rate = kokoro.create(
        "This is a Kokoro voice test on this machine.", voice="af_heart", speed=1.0
    )
    wall_s = time.monotonic() - started
    out = settings.models_dir / "smoke_test.wav"
    sf.write(str(out), samples, sample_rate)
    audio_s = len(samples) / sample_rate
    return out, audio_s, wall_s
```

(If Task 6 unexpectedly required an alternative TTS stack, implement the same signature against it and note the difference in the spike log.)

Extend the `setup` command in `src/videomaker/cli.py` — after the `ensure_models` loop, add:

```python
    from videomaker.setup_cmd import spike_tts

    console.print("Running Kokoro TTS smoke test...")
    wav, audio_s, wall_s = spike_tts(settings)
    rtf = wall_s / audio_s if audio_s else float("inf")
    console.print(
        f"  [green]OK[/green] {wav} ({audio_s:.1f}s audio in {wall_s:.1f}s, RTF {rtf:.2f})"
    )
```

- [x] **Step 2: Run it for real**

Run: `uv run videomaker setup`
Expected: prints the smoke line with an RTF number; `xdg-open ~/.cache/ai-video-maker/models/smoke_test.wav` plays an intelligible female voice saying the test sentence. RTF (wall/audio) on the i7-1355U is expected well under 1.0 (likely 0.1–0.5) — record the actual number.

- [x] **Step 3: Verify nothing else broke**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all pass.

- [x] **Step 4: Update spike log and commit**

Fill in "Kokoro TTS smoke test" in the spike log: RTF, voice used, subjective quality note.

```bash
git add src/videomaker/setup_cmd.py src/videomaker/cli.py docs/superpowers/spike-results-m0.md
git commit -s -m "feat: kokoro TTS smoke test in setup; record RTF"
```

---

### Task 8: faster-whisper STT smoke test in setup

**Files:**
- Modify: `src/videomaker/setup_cmd.py` (add `spike_stt`), `src/videomaker/cli.py` (call it from `setup`)
- Test: manual on this Mac (as Task 7)

**Interfaces:**
- Consumes: the smoke wav from `spike_tts`.
- Produces: `spike_stt(settings: Settings, wav: Path) -> list[tuple[str, float, float]]` — (word, start_s, end_s) tuples. M1's STT provider is written against the same `faster_whisper` API proven here.

- [x] **Step 1: Implement spike_stt**

Append to `src/videomaker/setup_cmd.py`:

```python
def spike_stt(settings: Settings, wav: Path) -> list[tuple[str, float, float]]:
    """Transcribe the smoke wav with word timestamps; returns (word, start, end) tuples."""
    from faster_whisper import WhisperModel

    model = WhisperModel(
        "base", device="cpu", compute_type="int8",
        download_root=str(settings.models_dir / "whisper"),
    )
    segments, _info = model.transcribe(str(wav), word_timestamps=True)
    words: list[tuple[str, float, float]] = []
    for segment in segments:
        for word in segment.words or []:
            words.append((word.word.strip(), word.start, word.end))
    return words
```

Extend the `setup` command in `src/videomaker/cli.py` — after the TTS smoke block, add:

```python
    from videomaker.setup_cmd import spike_stt

    console.print("Running faster-whisper STT smoke test (downloads base model on first run)...")
    words = spike_stt(settings, wav)
    preview = " ".join(w for w, _, _ in words[:6])
    console.print(f"  [green]OK[/green] {len(words)} words with timestamps: '{preview} ...'")
```

**Fallback note** (record outcome either way): faster-whisper/ctranslate2 ship manylinux wheels, so failures are unexpected on the HP. If `import faster_whisper` nevertheless crashes, record it and pin the older pair `uv add --optional ml "faster-whisper==0.10.1" "ctranslate2==3.24.0"`; the last-resort whisper-cli subprocess provider is documented in `docs/macos-intel-notes.md` and would apply here analogously — do not build it in M0.

- [x] **Step 2: Run it for real**

Run: `uv run videomaker setup`
Expected: whisper base model downloads (~145 MB, first run), then prints ~9 words with timestamps matching the smoke sentence ("this is a kokoro voice test...").

- [x] **Step 3: Verify nothing else broke**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all pass.

- [x] **Step 4: Update spike log and commit**

Fill in "faster-whisper / ctranslate2 wheels" in the spike log: versions, transcription accuracy of the smoke line, wall time.

```bash
git add src/videomaker/setup_cmd.py src/videomaker/cli.py pyproject.toml uv.lock \
        docs/superpowers/spike-results-m0.md
git commit -s -m "feat: faster-whisper word-timestamp smoke test in setup"
```

---

### Task 9: CI workflow + community/legal files

**Files:**
- Create: `.github/workflows/ci.yml`, `LICENSE`, `README.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`, `NOTICE.md`, `docs/macos-intel-notes.md`

**Interfaces:**
- Produces: green CI on every push (lint + unit tests, no ML extra, no model downloads); the AGPL licensing footprint required before the repo can ever go public.

- [x] **Step 1: Write the CI workflow**

`.github/workflows/ci.yml`:

```yaml
name: ci
on:
  push:
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with:
          python-version: "3.12"
      - name: Install (no ml extra — CI never runs model inference)
        run: uv sync
      - name: Lint
        run: uv run ruff check .
      - name: Unit tests
        run: uv run pytest -q
```

- [x] **Step 2: Fetch the license texts verbatim**

```bash
curl -fsSL https://www.gnu.org/licenses/agpl-3.0.txt -o LICENSE
curl -fsSL https://www.contributor-covenant.org/version/2/1/code_of_conduct/code_of_conduct.md -o CODE_OF_CONDUCT.md
```

Verify: `head -3 LICENSE` shows "GNU AFFERO GENERAL PUBLIC LICENSE / Version 3"; `head -3 CODE_OF_CONDUCT.md` shows "Contributor Covenant Code of Conduct".

- [x] **Step 3: Write README.md**

```markdown
# AI Video Maker

Local-first, human-in-the-loop AI video studio. Turns a topic into a finished
long-form YouTube video **and** a 9:16 Short/Reel from the same project —
using only free/open models (Kokoro TTS, faster-whisper) and free API tiers
(Groq/Gemini scripts, Pexels footage, Cloudflare Workers AI images).

**Status: pre-alpha.** M0 (scaffold + environment doctor) is done; the video
pipeline lands in M1+. See `docs/superpowers/specs/` for the full design.

## Why another AI video tool?

Platforms now demonetize mass-produced templated AI content. This tool is the
opposite of a one-click bot: the pipeline pauses at three review gates
(script → storyboard → preview) so every video is individually curated.
Intended for original creator content — not spam farms or misinformation.

## Quickstart (Linux, Debian/Ubuntu-based)

```bash
sudo apt install -y ffmpeg   # distro builds include libass (captions)
curl -LsSf https://astral.sh/uv/install.sh | sh   # if uv isn't installed
git clone <repo-url> && cd AI-video-maker
uv sync --extra ml
cp .env.example .env         # fill in the free API keys (links inside)
uv run videomaker setup      # downloads models (~500 MB), runs smoke tests
uv run videomaker doctor     # everything should be OK/WARN, no FAIL
```

macOS users (including Intel Macs): see `docs/macos-intel-notes.md`.

## License

AGPL-3.0-only. The project name is reserved by the maintainer; forks should
use their own name. Contributions require DCO sign-off (`git commit -s`).
```

- [x] **Step 4: Write CONTRIBUTING.md, SECURITY.md, NOTICE.md**

`CONTRIBUTING.md`:

```markdown
# Contributing

## Setup

Requires [uv](https://docs.astral.sh/uv/) and FFmpeg with libass
(`brew install ffmpeg` on macOS, `apt install ffmpeg` on Debian/Ubuntu).

```bash
uv sync --extra ml
uv run pytest        # unit tests (no API keys or models needed)
uv run ruff check .
```

## Rules

- **DCO**: sign off every commit (`git commit -s`). No CLA.
- **TDD**: new behavior arrives with a failing test first.
- **Niche templates** are YAML files in `templates/` — the easiest way to
  contribute, no Python needed (schema docs land in M4).
- **Providers** (LLM/TTS/STT/stock/image/upload) implement the ABCs in
  `src/videomaker/providers/base.py` (lands in M1). Free-tier-friendly
  providers are preferred; paid providers must be optional.
- Keep the $0-by-default promise: never make a paid service required.
```

`SECURITY.md`:

```markdown
# Security

Report vulnerabilities privately to claudekwizera003@gmail.com.
Do not open public issues for security problems. Best-effort response
within 7 days. The web UI binds 127.0.0.1 by default and has no
authentication — never expose it directly to the internet.
```

`NOTICE.md`:

```markdown
# Third-party notices

- **Kokoro-82M** TTS model — Apache-2.0 (hexgrad/Kokoro-82M); ONNX runtime
  packaging via kokoro-onnx (MIT).
- **faster-whisper** — MIT; Whisper models by OpenAI (MIT).
- **FFmpeg** — LGPL/GPL, invoked as a subprocess (not linked).
- **Pexels / Pixabay** media: free licenses including commercial use;
  attribution is appreciated and auto-generated per video (M5).
- **Groq, Google Gemini, Cloudflare Workers AI**: bring-your-own-key;
  usage is subject to each provider's terms.
- Bundled fonts (added in M3) are SIL OFL licensed; see assets/fonts/OFL.txt.
```

- [x] **Step 5: Write docs/macos-intel-notes.md (preserves the Mac research for contributors)**

`docs/macos-intel-notes.md`:

```markdown
# macOS (Intel) notes

The app targets Linux first but runs on Macs. Two caveats verified on a 2018
Intel MacBook Pro (i5-8259U):

1. **FFmpeg must include libass.** Some Homebrew setups have a lean ffmpeg
   build without the `subtitles`/`drawtext` filters — caption burning fails.
   Fix: `brew reinstall ffmpeg` (the standard bottle links libass).
   `videomaker doctor` detects this and prints the remediation.
2. **Python wheels on darwin x86_64.** PyTorch ships no Intel-Mac wheels after
   2.2.2 — this is why the default TTS is kokoro-onnx, not the torch-based
   kokoro package. If `uv sync --extra ml` fails resolving onnxruntime or
   ctranslate2, pin older versions in this order:
   - `uv add --optional ml "onnxruntime==1.19.2"` (then try 1.18.1)
   - `uv add --optional ml "faster-whisper==0.10.1" "ctranslate2==3.24.0"`
   - TTS last resort: swap kokoro-onnx for sherpa-onnx (bundles its runtime,
     supports Kokoro voices).
   - STT last resort: `brew install whisper-cpp` and use the whisper-cli
     subprocess provider (planned as a fallback STTProvider).
3. Hardware fast-render uses `h264_videotoolbox` when present (auto-detected
   by `videomaker doctor`); on Apple Silicon none of the wheel issues apply.
```

- [x] **Step 6: Verify and commit**

Run: `uv run pytest -q && uv run ruff check .` → all pass.

```bash
git add .github/ LICENSE README.md CONTRIBUTING.md CODE_OF_CONDUCT.md SECURITY.md NOTICE.md docs/macos-intel-notes.md
git commit -s -m "chore: CI workflow, AGPL license, community files, macOS notes"
```

(The private GitHub remote already exists — CI executes on push; check the Actions tab after pushing.)

---

### Task 10: M0 definition-of-done verification

**Files:**
- Modify: `docs/superpowers/spike-results-m0.md` (final summary section)

**Interfaces:**
- Produces: the verified green baseline M1's plan builds on.

- [x] **Step 1: Full verification battery**

Run each and confirm:

```bash
uv run pytest -q                 # expected: all tests pass
uv run ruff check .              # expected: no errors
uv run videomaker version        # expected: 0.0.1
uv run videomaker doctor         # expected: exit 0; ffmpeg+subtitles OK, python OK,
                                 #           models OK, disk OK; only API-key WARNs allowed
uv run videomaker setup          # expected: "ready" for cached models (no re-download),
                                 #           TTS smoke OK with RTF, STT smoke OK with words
```

- [x] **Step 2: Append the M0 summary to the spike log**

Add to `docs/superpowers/spike-results-m0.md`:

```markdown
## M0 summary (definition of done)
- doctor: all green (API keys warn only) — date: <fill in>
- TTS stack for M1: <kokoro-onnx + onnxruntime==X.Y.Z | sherpa-onnx>
- STT stack for M1: <faster-whisper==X + ctranslate2==Y | whisper-cli fallback>
- Kokoro RTF on this machine: <n.nn>
- Open follow-ups for M1: <anything discovered, or "none">
```

- [x] **Step 3: Commit**

```bash
git add docs/superpowers/spike-results-m0.md
git commit -s -m "docs: M0 complete — spike results and green doctor baseline"
```

---

## Self-review notes

- **Spec coverage (M0 scope)**: scaffold ✓ (T1), pyproject/uv/pin ✓ (T1), community files ✓ (T9), CI ✓ (T9), doctor with subtitles-filter probe + remediation ✓ (T4–T5), setup with model downloads + TTS/STT smoke ✓ (T6–T8), spike outcomes recorded for M1 ✓ (T5/T6/T8/T10), macOS contributor notes preserving the Intel-Mac research ✓ (T9). Deliberately out of M0 scope per spec: models.py schemas, providers, pipeline, web UI (all M1+).
- **Types consistent**: `Settings` fields used in T4/T6 match T2's definition; `FFmpegCaps` fields in T4's tests match T3 (`hw_encoder: str`, platform-neutral); `spike_tts` return tuple consumed in T8's CLI wiring matches T7.
- **Platform retarget note**: originally drafted for the Intel MacBook; retargeted 2026-08-30 to the HP ProBook 450 G10 (Zorin OS 18, Linux x86_64) after the owner chose to migrate. Wheel risks that were M0's main concern on macOS are near-zero on Linux; the smoke tests remain as cheap verification, and the Mac knowledge ships in `docs/macos-intel-notes.md` for future contributors.
