from typing import NoReturn

import typer
from rich.console import Console
from rich.table import Table

from videomaker import __version__

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()

LEVEL_STYLE = {"ok": "green", "warn": "yellow", "fail": "red"}

#: Exit codes are the contract for anything scripting this CLI: 0 success,
#: 1 a stage (or the command) failed, 2 stopped at an unapproved review gate.
EXIT_ERROR = 1
EXIT_GATE = 2


def _fail(message: str) -> NoReturn:
    console.print(f"[red]error[/red] {message}")
    raise typer.Exit(code=EXIT_ERROR)


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

    from videomaker.setup_cmd import spike_tts

    console.print("Running Kokoro TTS smoke test...")
    wav, audio_s, wall_s = spike_tts(settings)
    rtf = wall_s / audio_s if audio_s else float("inf")
    console.print(
        f"  [green]OK[/green] {wav} ({audio_s:.1f}s audio in {wall_s:.1f}s, RTF {rtf:.2f})"
    )

    from videomaker.setup_cmd import spike_stt

    console.print("Running faster-whisper STT smoke test (downloads base model on first run)...")
    words = spike_stt(settings, wav)
    preview = " ".join(w for w, _, _ in words[:6])
    console.print(f"  [green]OK[/green] {len(words)} words with timestamps: '{preview} ...'")
    console.print("Setup complete. Run [bold]videomaker doctor[/bold] to verify.")


# --------------------------------------------------------------------------- M1


@app.command()
def new(
    topic: str = typer.Argument(..., help="What the video explains, in plain words."),
    template: str = typer.Option("tech_explainer", "-t", "--template", help="Template name."),
    minutes: float = typer.Option(2.0, "-m", "--minutes", help="Target length in minutes."),
    voice: str = typer.Option("af_heart", "--voice", help="TTS voice id."),
) -> None:
    """Create a new project folder and print its id."""
    from videomaker.config import load_settings
    from videomaker.project import ProjectStore
    from videomaker.templates import load_template

    settings = load_settings()
    try:
        # Fail here rather than three stages later, and list what is available.
        load_template(template)
    except ValueError as exc:
        _fail(str(exc))

    store = ProjectStore(settings.workspace_dir)
    project = store.create(topic, template, target_minutes=minutes, voice=voice)
    console.print(f"created [bold]{project.id}[/bold] in {store.path_for(project.id)}")
    console.print(f"next: [bold]videomaker run {project.id} --yes[/bold]")


@app.command()
def run(
    project_id: str = typer.Argument(..., help="Project id, as printed by `new`."),
    yes: bool = typer.Option(False, "--yes", help="Approve every review gate as it is reached."),
    until: str | None = typer.Option(None, "--until", help="Stop after this stage."),
    providers: str | None = typer.Option(
        None, "--providers", help="Force every provider kind to this one (e.g. `mock`)."
    ),
) -> None:
    """Run the pipeline: script, voice, align, visuals, captions, assemble, render."""
    from videomaker.config import load_settings
    from videomaker.pipeline.base import StageResult
    from videomaker.runner import (
        GateBlocked,
        StageFailed,
        build_deps,
        derive_status,
        provider_override,
        run_pipeline,
    )

    settings = load_settings()
    if providers:
        settings = provider_override(settings, providers)

    deps = build_deps(settings, project_id)
    try:
        project = deps.store.load(project_id)
    except FileNotFoundError as exc:
        _fail(str(exc))

    def report(stage: str, result: StageResult) -> None:
        verb = "[green]ran[/green]" if result.changed else "[dim]cached[/dim]"
        console.print(f"  {verb} {stage} ({result.skipped_units} unit(s) skipped)")

    try:
        run_pipeline(project, deps, until=until, yes=yes, on_stage=report)
    except GateBlocked as exc:
        console.print(f"[yellow]blocked[/yellow] at the {exc.gate} gate: review {exc.review}")
        console.print(f"status: [bold]{exc.status.value}[/bold]")
        console.print(f"approve with [bold]videomaker run {project_id} --yes[/bold]")
        raise typer.Exit(code=EXIT_GATE) from exc
    except StageFailed as exc:
        _fail(str(exc))
    except ValueError as exc:  # an unknown --until stage
        _fail(str(exc))

    console.print(f"status: [bold]{derive_status(project, deps.stage_cache).value}[/bold]")


@app.command()
def status(
    project_id: str = typer.Argument(..., help="Project id, as printed by `new`."),
) -> None:
    """Show the derived status, stage by stage."""
    from videomaker.cache import STAGE_ORDER
    from videomaker.config import load_settings
    from videomaker.project import ProjectStore
    from videomaker.runner import GATE_BEFORE, derive_status, stage_cache_for, stage_is_current

    store = ProjectStore(load_settings().workspace_dir)
    try:
        project = store.load(project_id)
    except FileNotFoundError as exc:
        _fail(str(exc))
    cache = stage_cache_for(store, project_id)

    console.print(f"[bold]{project.id}[/bold] — {project.topic} ({project.template})")
    console.print(f"status: [bold]{derive_status(project, cache).value}[/bold]")

    table = Table()
    table.add_column("stage")
    table.add_column("state")
    table.add_column("gate")
    for stage in STAGE_ORDER:
        current = stage_is_current(project, cache, stage)
        state = "[green]current[/green]" if current else "[yellow]pending[/yellow]"
        gate = GATE_BEFORE.get(stage, "")
        if gate:
            approved = getattr(project.approvals, gate)
            gate = f"{gate}: " + ("approved" if approved else "[yellow]waiting[/yellow]")
        table.add_row(stage, state, gate)
    console.print(table)


@app.command("list")
def list_projects() -> None:
    """List every project in the workspace with its derived status."""
    from videomaker.config import load_settings
    from videomaker.project import ProjectStore
    from videomaker.runner import derive_status, stage_cache_for

    store = ProjectStore(load_settings().workspace_dir)
    table = Table(title="projects")
    table.add_column("id")
    table.add_column("status")
    table.add_column("topic")
    for project_id in store.list_ids():
        try:
            project = store.load(project_id)
        except (FileNotFoundError, ValueError):
            table.add_row(project_id, "[red]unreadable[/red]", "")
            continue
        cache = stage_cache_for(store, project_id)
        table.add_row(project_id, derive_status(project, cache).value, project.topic)
    console.print(table)


def main() -> None:
    app()
