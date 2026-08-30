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


def main() -> None:
    app()
