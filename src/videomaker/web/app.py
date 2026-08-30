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

from fastapi import FastAPI

from videomaker import __version__
from videomaker.config import Settings, load_settings
from videomaker.project import ProjectStore
from videomaker.runner import provider_override
from videomaker.web import media
from videomaker.web.worker import JobQueue


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

    app.include_router(media.router)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

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
