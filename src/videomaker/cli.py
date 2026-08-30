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
