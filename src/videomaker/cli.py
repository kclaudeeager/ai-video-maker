from pathlib import Path
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
    folder: str = typer.Option(
        "", "--folder", help="File it under this label, e.g. `tech/office-basics`."
    ),
) -> None:
    """Create a new project folder and print its id."""
    from videomaker.config import load_settings
    from videomaker.models import clean_folder
    from videomaker.project import ProjectStore
    from videomaker.templates import load_template

    settings = load_settings()
    try:
        # Fail here rather than three stages later, and list what is available.
        load_template(template)
    except ValueError as exc:
        _fail(str(exc))

    # The label is metadata, but it is metadata a person types. Refusing a
    # path-shaped one here means no `project.json` on disk ever carries `../..` for
    # some later code to be careless with. See `models.clean_folder`.
    try:
        folder = clean_folder(folder)
    except ValueError as exc:
        _fail(str(exc))

    # M3 Task 21: the web form refuses a voice whose language this stack was
    # measured to get wrong. The CLI is the deliberate escape hatch — someone
    # re-running the measurement needs to be able to make one — so it warns and
    # obeys rather than refusing. What it must not do is stay silent.
    from videomaker.web.voices import refusal_for

    if refusal := refusal_for(voice):
        console.print(f"[yellow]warning:[/yellow] {refusal}")

    store = ProjectStore(settings.workspace_dir)
    project = store.create(topic, template, target_minutes=minutes, voice=voice, folder=folder)
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


def _in_folder(folder: str, wanted: str) -> bool:
    """True when `folder` is `wanted` or sits under it.

    `--folder tech` means "the tech shelf", boxes included, so `tech/office-basics`
    matches. It is a level-wise prefix and not a string one: `technology` is a
    different shelf that merely starts with the same letters. `--folder ""` is the
    root, which is the one place that is *not* inclusive — it means the unfiled
    projects, not all of them.
    """
    if not wanted:
        return not folder
    return folder == wanted or folder.startswith(f"{wanted}/")


@app.command("list")
def list_projects(
    folder: str | None = typer.Option(
        None, "--folder", help="Only projects filed here or below. Pass '' for the root."
    ),
) -> None:
    """List every project in the workspace with its derived status."""
    from videomaker.config import load_settings
    from videomaker.models import clean_folder
    from videomaker.project import ProjectStore
    from videomaker.runner import derive_status, stage_cache_for

    if folder is not None:
        try:
            folder = clean_folder(folder)
        except ValueError as exc:
            _fail(str(exc))

    store = ProjectStore(load_settings().workspace_dir)
    table = Table(title="projects" if folder is None else f"projects in {folder or 'the root'}")
    table.add_column("id")
    table.add_column("status")
    table.add_column("folder")
    table.add_column("topic")
    for project_id in store.list_ids():
        try:
            project = store.load(project_id)
        except (FileNotFoundError, ValueError):
            if folder is None:
                table.add_row(project_id, "[red]unreadable[/red]", "", "")
            continue
        if folder is not None and not _in_folder(project.folder, folder):
            continue
        cache = stage_cache_for(store, project_id)
        table.add_row(project_id, derive_status(project, cache).value, project.folder, project.topic)
    console.print(table)


def _is_loopback(host: str) -> bool:
    """True only for addresses that cannot be reached from another machine."""
    import ipaddress

    if host in {"localhost", "localhost."}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # A name we cannot resolve to a literal (or "" / "*") — assume exposed.
        return False


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Address to bind. Keep it loopback."),
    port: int = typer.Option(8000, "--port", help="Port to listen on."),
    providers: str | None = typer.Option(
        None, "--providers", help="Force every provider kind to this one (e.g. `mock`)."
    ),
    reload: bool = typer.Option(False, "--reload", help="Restart on code changes (development)."),
) -> None:
    """Serve the review web UI at http://HOST:PORT."""
    import os

    import uvicorn

    from videomaker.web.app import PROVIDERS_ENV_VAR, create_app

    if not _is_loopback(host):
        console.print(
            f"[bold red]WARNING[/bold red] binding {host}, which is NOT a loopback address: "
            "this server is reachable from other machines on the network."
        )
        console.print(
            "[bold red]WARNING[/bold red] there is NO authentication — "
            "anyone who can reach this port can read and write any path your user account can, "
            "and can run the pipeline (spending your provider quota)."
        )
        console.print(
            "[bold red]WARNING[/bold red] only do this on a network you trust, "
            "and stop the server when you are done."
        )

    console.print(f"serving the review UI on [bold]http://{host}:{port}[/bold] (Ctrl-C to stop)")
    if reload:
        # uvicorn's reloader re-imports the app in a child process, so it only
        # accepts an import string; `--providers` travels in the environment.
        if providers:
            os.environ[PROVIDERS_ENV_VAR] = providers
        uvicorn.run(
            "videomaker.web.app:create_app_from_env",
            host=host,
            port=port,
            factory=True,
            reload=True,
        )
        return
    uvicorn.run(create_app(providers=providers), host=host, port=port)


# --------------------------------------------------------------------------- M3

music_app = typer.Typer(
    no_args_is_help=True,
    help="Inspect your own music and SFX library (the project ships no audio).",
)
app.add_typer(music_app, name="music")

EMPTY_LIBRARY_LINES = (
    "an empty library is [bold]not an error[/bold]: the pipeline renders narration only.",
    (
        "the project ships no audio files — drop your own into "
        "[bold]assets/music/<mood>/[/bold] and [bold]assets/sfx/<role>/[/bold], "
        "then record them in assets/music/library.yaml."
    ),
    "where to get licence-clear audio: see [bold]assets/music/README.md[/bold].",
)


def _scan_library(*, index_path: Path | None):
    """The library as it is on disk right now, with the index used only for durations."""
    from videomaker.audio import scan
    from videomaker.config import load_settings

    settings = load_settings()
    return settings, scan(settings.music_dir, settings.sfx_dir, index_path=index_path)


@music_app.command("scan")
def music_scan() -> None:
    """Index assets/music and assets/sfx (ffprobe for duration) into the local cache."""
    from videomaker import audio

    index_path = audio.DEFAULT_INDEX_PATH
    settings, library = _scan_library(index_path=index_path)
    audio.write_index(library, index_path)

    console.print(f"scanned {settings.music_dir} and {settings.sfx_dir}")
    if not library:
        console.print("  [yellow]no audio found[/yellow]")
        for line in EMPTY_LIBRARY_LINES:
            console.print(f"  {line}")
    else:
        console.print(
            f"  [green]indexed[/green] {len(library.tracks)} file(s) "
            f"({library.probed} probed, {library.reused} from the cache)"
        )
        console.print(
            f"  music: {len(library.music())} in {', '.join(library.moods()) or '-'}   "
            f"sfx: {len(library.sfx())} in {', '.join(library.roles()) or '-'}"
        )
    _print_library_warnings(library)
    console.print(f"index: {index_path}")


@music_app.command("list")
def music_list() -> None:
    """Print the music and SFX library. Reads the index when it is fresh, disk always."""
    from videomaker import audio

    _, library = _scan_library(index_path=audio.DEFAULT_INDEX_PATH)
    if not library:
        console.print("the audio library is [bold]empty[/bold].")
        for line in EMPTY_LIBRARY_LINES:
            console.print(line)
        return

    table = Table(title="audio library")
    table.add_column("kind")
    table.add_column("group")
    table.add_column("file")
    table.add_column("length", justify="right")
    table.add_column("licence")
    table.add_column("credit")
    for track in library.tracks:
        credit = track.attribution.credit_line() if track.attribution else ""
        table.add_row(
            track.kind,
            track.group or "[dim]-[/dim]",
            track.name,
            audio.format_duration(track.duration_s),
            track.attribution.licence if track.attribution else "",
            credit or "[yellow]not in library.yaml[/yellow]",
        )
    console.print(table)
    _print_library_warnings(library)
    if library.probed:
        console.print(
            f"[dim]{library.probed} file(s) were probed because the index is stale or "
            "missing; run [bold]videomaker music scan[/bold] to refresh it.[/dim]"
        )


def _print_library_warnings(library) -> None:
    """Everything the library wants to say. All of it advisory — none of it fails."""
    for line in library.warnings():
        console.print(f"[yellow]warning[/yellow] {line}")


def main() -> None:
    app()
