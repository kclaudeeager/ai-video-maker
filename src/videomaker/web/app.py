"""The FastAPI application factory for the review web UI.

`create_app` is a factory rather than a module-level singleton so that tests (and
a future embedded use) can build isolated apps over their own workspace, each
with its own `Settings` and `ProjectStore` hanging off `app.state`.

The background worker is *constructed* here but only *started* by the lifespan.
That split matters: `TestClient(app)` used without its context manager never runs
lifespan, so a queue that started itself in the factory would leak a thread out of
every such test, while routes still need `app.state.jobs` to exist to submit to.
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from videomaker import __version__
from videomaker.assets import ASSETS_DIR
from videomaker.config import Settings, load_settings
from videomaker.project import ProjectStore
from videomaker.runner import provider_override
from videomaker.web import media
from videomaker.web.auth import PASSWORD_ENV, PasswordGate
from videomaker.web.routes import projects, render, script, storyboard
from videomaker.web.worker import JobQueue

#: Templates and static assets live inside the package, not at the repo root, so
#: that `[tool.hatch.build.targets.wheel] packages = ["src/videomaker"]` carries
#: them into the wheel and an installed copy works with no source tree present.
#: Anchoring on `__file__` (rather than the CWD) is what makes that true.
_WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = _WEB_DIR / "templates"
STATIC_DIR = _WEB_DIR / "static"


def create_app(settings: Settings | None = None, *, providers: str | None = None) -> FastAPI:
    """Build a review-UI app over `settings` (loaded from config.yaml when omitted).

    `providers` mirrors the CLI's `--providers`: it forces every provider kind to
    that one name, which is how the tests and the offline golden path stay away
    from the network.
    """
    settings = settings if settings is not None else load_settings()
    if providers:
        settings = provider_override(settings, providers)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.jobs.start()
        try:
            yield
        finally:
            # Joins the worker, so a test client leaves no thread behind. A job
            # already running is given `stop`'s timeout and no longer than that.
            app.state.jobs.stop()

    app = FastAPI(title="AI Video Maker", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.store = ProjectStore(settings.workspace_dir)
    app.state.jobs = JobQueue()

    # Starlette's own static handling normalises the request path and refuses
    # to leave the directory, so `/static` needs no guard of its own — unlike
    # `/media`, which serves an arbitrary workspace path (see `web.media`).
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    # The bundled OFL fonts, in their own package directory rather than under
    # `static/`: thumbnail rendering (M3 Task 15) reads the same files off disk
    # and should not have to reach through the web package to find them.
    app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")
    app.state.templates = Jinja2Templates(directory=TEMPLATES_DIR)

    app.include_router(media.router)
    app.include_router(projects.router)
    # After `projects`, whose `/projects/{project_id}` would otherwise be a
    # candidate for nothing here — the paths are disjoint, but keeping the
    # dashboard first states the intended precedence rather than relying on it.
    app.include_router(script.router)
    # Gate 2. Its paths are disjoint from gate 1's — `/scenes/{id}/choose`,
    # `/motion`, `/revoice` and `/approve/storyboard` — so the order is a
    # statement of pipeline order rather than a precedence the app depends on.
    app.include_router(storyboard.router)
    # Gate 3, and the render progress page. Registered last because it is last in
    # the pipeline, not because anything depends on the order: `/preview`,
    # `/preview/build`, `/approve/preview` and `/render` collide with nothing
    # above them.
    app.include_router(render.router)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    # Last, so it wraps every route and both mounted `StaticFiles` apps. Absent
    # when no password is set, which is the local single-user case: `serve`
    # refuses a non-loopback bind without one, so an unset password can only
    # mean loopback. See `web.auth`.
    password = os.environ.get(PASSWORD_ENV, "")
    if password:
        app.add_middleware(PasswordGate, password=password)

    return app


#: How `videomaker serve --reload` passes `--providers` through to the reloader:
#: uvicorn's reloader re-imports the app in a fresh process, so the only channel
#: from the parent is the environment.
PROVIDERS_ENV_VAR = "VIDEOMAKER_PROVIDERS"


def create_app_from_env() -> FastAPI:
    """An import-string entry point (`videomaker.web.app:create_app_from_env`).

    uvicorn's `--reload` refuses anything but an import string, and the subprocess
    it spawns cannot see our `Settings`; it reads `VIDEOMAKER_PROVIDERS` instead.
    """
    return create_app(providers=os.environ.get(PROVIDERS_ENV_VAR) or None)
