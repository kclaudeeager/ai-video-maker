"""`/start` — the three ways into the workspace, and the page behind each one.

The root asks what you want to work on; this module answers. Each way in starts
from a different thing, and the page behind it is a form over exactly that thing:
a sentence, a text in the catalogue, a file on your disk.

**Two of the three end in a library work and one ends in a `Project`,** and that
asymmetry is the design rather than an oversight — see
`docs/superpowers/plans/2026-09-10-unified-workspace.md`. Nothing here gives
reading a state machine.

The video form is *moved*, not rewritten: `POST /projects` keeps its path,
its validation and its 303, and `projects.render_new_project` is what draws it.
"""

import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from videomaker.config import Settings
from videomaker.corpus.catalogue import CATALOGUE
from videomaker.corpus.documents import (
    DOCUMENT_SUFFIXES,
    DocumentSpec,
    import_document,
    suggest_id,
)
from videomaker.corpus.importer import ImportSpec, import_work, list_works
from videomaker.web.routes.projects import render_new_project
from videomaker.web.worker import JobQueue

router = APIRouter()

#: An upload has to be written to disk before it can be parsed, so it has to have
#: a ceiling. 32 MB is several times the largest text this reader is meant for —
#: the whole World English Bible is ~5 MB of USFM — and small enough that a
#: mistaken drag of a video file is refused rather than filling the disk.
MAX_UPLOAD_BYTES = 32 * 1024 * 1024

#: Read in chunks so the ceiling is enforced *while* writing rather than after.
_UPLOAD_CHUNK = 1 << 20


def _templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


@router.get("/start")
def start_page(request: Request):
    """The three ways in, on their own page — the same three the root shows."""
    return _templates(request).TemplateResponse(request, "start.html", {})


# ------------------------------------------------------------------ from an idea


@router.get("/start/video")
def start_video(request: Request):
    return render_new_project(request)


# -------------------------------------------------------------------- from a work


def _catalogue_rows(settings: Settings) -> list[dict[str, object]]:
    """Every blessed text, and whether it is already here.

    The fixture row is deliberately included: it is what the acceptance script
    imports, and hiding it would mean the page and the CLI disagree about what
    the catalogue holds.
    """
    imported = {work.id for work, _chapters in list_works(settings.workspace_dir)}
    return [
        {"spec": spec, "imported": work_id in imported}
        for work_id, spec in sorted(CATALOGUE.items())
    ]


@router.get("/start/read")
def start_read(request: Request, error: str = ""):
    settings: Settings = request.app.state.settings
    return _templates(request).TemplateResponse(
        request,
        "start_read.html",
        {"rows": _catalogue_rows(settings), "error": error},
    )


@router.post("/start/read")
def import_from_catalogue(request: Request, work_id: str = Form("")):
    """Fetch one blessed text, on the job queue — it is a download and a parse.

    The gate is `ImportSpec`'s, unchanged: a row without a stateable licence
    cannot exist in the catalogue, so there is nothing to check again here.
    """
    settings: Settings = request.app.state.settings
    spec = CATALOGUE.get(work_id)
    if spec is None:
        return _templates(request).TemplateResponse(
            request,
            "start_read.html",
            {"rows": _catalogue_rows(settings), "error": f"No text called {work_id!r}."},
            status_code=422,
        )
    if spec.archive is None:
        return _templates(request).TemplateResponse(
            request,
            "start_read.html",
            {
                "rows": _catalogue_rows(settings),
                "error": (
                    f"{spec.title} has no download of its own — it is the bundled "
                    "fixture. Import it with `videomaker library import fixture "
                    "--from tests/fixtures/usfm/`."
                ),
            },
            status_code=422,
        )
    jobs: JobQueue = request.app.state.jobs
    jobs.submit(f"import:{work_id}", "import", _import_job(settings, spec))
    return RedirectResponse(url=f"/library/{work_id}", status_code=303)


def _import_job(settings: Settings, spec: ImportSpec):
    def job(progress) -> None:
        progress.update(stage="import", progress=0.1, message=f"fetching {spec.title}")
        import_work(spec, settings.workspace_dir)
        progress.update(stage="import", progress=1.0, message="imported")

    return job


# --------------------------------------------------------------- from your own file


@router.get("/start/document")
def start_document(request: Request, error: str = ""):
    return _templates(request).TemplateResponse(
        request,
        "start_document.html",
        {"error": error, "suffixes": sorted(DOCUMENT_SUFFIXES)},
    )


def _refuse(request: Request, message: str):
    return _templates(request).TemplateResponse(
        request,
        "start_document.html",
        {"error": message, "suffixes": sorted(DOCUMENT_SUFFIXES)},
        status_code=422,
    )


@router.post("/start/document")
async def upload_document(
    request: Request,
    document: UploadFile,
    title: str = Form(""),
):
    """Take a file, import it as a work, and land on it.

    The upload is written to a temporary file rather than held in memory: the
    parsers read from a path, an EPUB is a zip that has to be seekable, and a
    32 MB ceiling in RAM per request is a worse trade than a file the `finally`
    removes. Every refusal comes back as the form with a reason on it.
    """
    settings: Settings = request.app.state.settings
    name = Path(document.filename or "").name
    suffix = Path(name).suffix.lower()
    if suffix not in DOCUMENT_SUFFIXES:
        offered = ", ".join(sorted(DOCUMENT_SUFFIXES))
        return _refuse(request, f"Longhand can read {offered}. {name or 'That file'} is not one.")

    staged = Path(tempfile.mkdtemp(prefix="longhand-upload-")) / name
    try:
        written = 0
        with staged.open("wb") as handle:
            while chunk := await document.read(_UPLOAD_CHUNK):
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    return _refuse(
                        request,
                        f"{name} is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB. "
                        "Longhand reads documents, not media.",
                    )
                handle.write(chunk)
        if written == 0:
            return _refuse(request, f"{name} is empty.")
        spec = DocumentSpec(
            work_id=suggest_id(name, taken={w.id for w, _c in list_works(settings.workspace_dir)}),
            title=title.strip() or Path(name).stem,
        )
        try:
            work = import_document(staged, spec, settings.workspace_dir)
        except (ValueError, ValidationError) as exc:
            return _refuse(request, str(exc))
    finally:
        shutil.rmtree(staged.parent, ignore_errors=True)
    return RedirectResponse(url=f"/library/{work.id}", status_code=303)
