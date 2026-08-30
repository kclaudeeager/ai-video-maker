"""The FastAPI application factory for the review web UI.

`create_app` is a factory rather than a module-level singleton so that tests (and
a future embedded use) can build isolated apps over their own workspace, each
with its own `Settings` and `ProjectStore` hanging off `app.state`.
"""

import os

from fastapi import FastAPI

from videomaker import __version__
from videomaker.config import Settings, load_settings
from videomaker.project import ProjectStore
from videomaker.runner import provider_override
from videomaker.web import media


def create_app(settings: Settings | None = None, *, providers: str | None = None) -> FastAPI:
    """Build a review-UI app over `settings` (loaded from config.yaml when omitted).

    `providers` mirrors the CLI's `--providers`: it forces every provider kind to
    that one name, which is how the tests and the offline golden path stay away
    from the network.
    """
    settings = settings if settings is not None else load_settings()
    if providers:
        settings = provider_override(settings, providers)

    app = FastAPI(title="AI Video Maker", version=__version__)
    app.state.settings = settings
    app.state.store = ProjectStore(settings.workspace_dir)

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
