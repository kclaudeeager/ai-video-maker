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
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from videomaker import __version__
from videomaker.assets import ASSETS_DIR
from videomaker.config import Audience, Settings, load_settings
from videomaker.project import ProjectStore
from videomaker.runner import provider_override
from videomaker.web import media
from videomaker.web.auth import PASSWORD_ENV, PasswordGate
from videomaker.web.routes import library, projects, render, script, start, storyboard
from videomaker.web.worker import JobQueue

#: Templates and static assets live inside the package, not at the repo root, so
#: that `[tool.hatch.build.targets.wheel] packages = ["src/videomaker"]` carries
#: them into the wheel and an installed copy works with no source tree present.
#: Anchoring on `__file__` (rather than the CWD) is what makes that true.
_WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = _WEB_DIR / "templates"
STATIC_DIR = _WEB_DIR / "static"


def create_app(
    settings: Settings | None = None,
    *,
    providers: str | None = None,
    dev: bool = False,
) -> FastAPI:
    """Build a review-UI app over `settings` (loaded from config.yaml when omitted).

    `providers` mirrors the CLI's `--providers`: it forces every provider kind to
    that one name, which is how the tests and the offline golden path stay away
    from the network.

    `dev` turns on Jinja's template auto-reload. Off by default so a running
    server cannot drift into rendering new templates against old handler code —
    see the note beside `app.state.templates`.
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
    # `auto_reload=False` unless we are explicitly in dev, and this is a
    # correctness fix rather than a performance one.
    #
    # Jinja re-reads a changed template off disk by default; the Python module
    # around it does not. A long-running server whose source has moved on
    # therefore renders **new templates against old route code**, and the failure
    # that produces is a 500 on an undefined variable — a template asking for a
    # context key the running handler was written before. That bit three times in
    # one afternoon (`next`, the folder routes, then `thumbnail`), and each time
    # it looked like a bug in the new code rather than a stale process.
    #
    # Frozen, the two halves stay in step: an old server serves a consistently old
    # page, which is obvious and harmless. Under `--reload` uvicorn replaces the
    # process on a source change, so dev keeps hot templates by asking for them.
    # Set on the environment rather than passed in: Starlette's constructor takes
    # a `directory` or a whole prebuilt `env`, and nothing in between.
    templates = Jinja2Templates(directory=TEMPLATES_DIR)
    templates.env.auto_reload = dev
    # `_nav.html` is included by `base.html` with no context of its own — it is
    # even rendered bare, with no request, by the template test — so the audience
    # reaches it as an environment global rather than as a context key nobody
    # could be relied on to pass.
    templates.env.globals["audience"] = settings.audience.value
    app.state.templates = templates

    # **Before `media`, and this order is load-bearing.** `media` owns
    # `/media/{project_id}/{path:path}`, which would otherwise swallow
    # `/media/reading/...` as a project literally named `reading` and 404 every
    # reading artefact. FastAPI matches in registration order, so the reader's
    # narrower route has to come first. The residual collision — a real project
    # named `reading` holding a file at `<anything>/reading.mp3` or
    # `.../reading.vtt`, the only two names the reader route will serve — is left
    # unhandled deliberately: guarding it would mean a lookup in the reader route
    # against the project store, which is a second, subtler path surface than the
    # one it would protect.
    app.include_router(library.router)
    app.include_router(media.router)

    # **What a reading server does not have.** `Audience.READER` mounts nothing
    # that can produce: no create form, no gates, no stage runner, no render. A
    # request for `/projects/...` on such a server is a 404 because there is no
    # route, not because a check said no — which is the difference between a
    # deployment choice and a permission system, and the reason this needs no
    # accounts. See `docs/superpowers/plans/2026-09-10-consumer-reading-view.md`.
    if settings.audience is Audience.READER:

        @app.get("/", include_in_schema=False)
        def _library_is_the_front_door() -> RedirectResponse:
            return RedirectResponse(url="/library", status_code=307)

        password = os.environ.get(PASSWORD_ENV, "")
        if password:
            app.add_middleware(PasswordGate, password=password)
        return app
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
    # `/start` and the three pages behind it. After `projects`, whose `/projects`
    # POST it links to and does not replace.
    app.include_router(start.router)

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

#: How `videomaker serve --reload --reader` reaches the reloader's child process,
#: for the same reason `PROVIDERS_ENV_VAR` exists: uvicorn re-imports the app in a
#: fresh process that cannot see our `Settings`.
AUDIENCE_ENV_VAR = "VIDEOMAKER_AUDIENCE"


def create_app_from_env() -> FastAPI:
    """An import-string entry point (`videomaker.web.app:create_app_from_env`).

    uvicorn's `--reload` refuses anything but an import string, and the subprocess
    it spawns cannot see our `Settings`; it reads `VIDEOMAKER_PROVIDERS` instead.

    This entry point exists only for `--reload`, so it is by definition dev: hot
    templates are wanted here, and uvicorn restarts the process under it anyway.
    """
    settings = load_settings()
    audience = os.environ.get(AUDIENCE_ENV_VAR)
    if audience in set(Audience):
        settings = settings.model_copy(update={"audience": Audience(audience)})
    return create_app(settings, providers=os.environ.get(PROVIDERS_ENV_VAR) or None, dev=True)
