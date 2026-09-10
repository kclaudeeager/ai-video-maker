"""The reader: a library, a work's outline, and one chapter in three modes.

**The reader creates no `Project` and enters no stage.** It reads the corpus and
synthesises from it directly, which is what makes the whole feature additive: no
route here can move a project's status, stale a stage or touch an approval.

Three things in this module are load-bearing rather than incidental:

* **`/media/reading/...` is the security-critical route.** `reading_key` and the
  filename are attacker-controlled, so both go through `web/media.py`'s existing
  guard — the syntactic layer that refuses dots-only segments and the semantic one
  that resolves and proves containment. There is deliberately no second copy of
  that logic here; the one in `web/media.py` is the one that has been reviewed.
* **A mode a work cannot support is absent, not broken.** `LISTEN` appears only
  when a configured `tts` provider lists the work's language
  (`docs/multimodal-reader-design.md` §6: a language with a text but no voice can
  be read, just not heard). The switcher is built from what is actually possible.
* **Building audio goes through the `JobQueue`,** like every other long job: the
  POST returns a polling fragment immediately. A chapter of forty verses is forty
  synthesis calls and a concat, which is not a request cycle.
"""

from enum import StrEnum
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.templating import Jinja2Templates

from videomaker.config import Settings
from videomaker.corpus.audio import (
    AUDIO_DIRNAME,
    Reading,
    build_reading,
    reader_deps,
    reading_key,
)
from videomaker.corpus.digest import Brief, build_brief
from videomaker.corpus.importer import DERIVED_DIRNAME, LIBRARY_DIRNAME, library_dir
from videomaker.corpus.models import UnitRef, UnitText, WorkRef
from videomaker.corpus.refs import BOOK_ORDER, book_name
from videomaker.providers.base import CorpusProvider, TTSProvider
from videomaker.providers.errors import ProviderError
from videomaker.providers.tts.http_api import HTTPTTSProvider, TTSBudgetExceeded, estimate_minutes
from videomaker.web import media
from videomaker.web.worker import JobQueue

router = APIRouter()

#: The job kind reader audio is submitted under, and the prefix its job key
#: carries. Job state is keyed by string and the reader has no project id, so the
#: reading's own reference is the key — prefixed so it can never collide with one.
READING_JOB_KIND = "reading"
JOB_PREFIX = "reading:"

#: Kept out of the `Reading` model: the browser asks for these by name and the
#: media route matches on them, so a request for anything else is refused before
#: a path is built at all.
SERVABLE = {"reading.mp3": "audio/mpeg", "reading.vtt": "text/vtt"}


class ReadMode(StrEnum):
    SOURCE = "source"
    BRIEF = "brief"
    LISTEN = "listen"


def job_key(ref: UnitRef, voice: str) -> str:
    """One job per chapter *and voice*: two voices are two different readings."""
    return f"{JOB_PREFIX}{ref.key()}:{voice}"


# ------------------------------------------------------------------ what is possible


def spoken_languages(settings: Settings) -> set[str]:
    """ISO codes a configured `tts` provider can actually speak.

    A YAML vendor states its languages outright. A local provider does not, so its
    voice ids are read through the same table `web/voices.py` uses — the voice
    prefix *is* the language (M3 Task 21), which is why there is no second list to
    keep in step. A provider that cannot be built at all contributes nothing rather
    than failing the page: an unconfigured voice is a missing mode, not an error.
    """
    from videomaker.providers import get_provider, resolve_chain
    from videomaker.providers.tts.kokoro_onnx import UNKNOWN_CODE, describe_voice

    codes: set[str] = set()
    for name in resolve_chain("tts", settings):
        try:
            provider = get_provider("tts", name, settings)
            if isinstance(provider, HTTPTTSProvider):
                codes.update(provider.cfg.languages)
                continue
            assert isinstance(provider, TTSProvider)
            for voice in provider.voices():
                code = describe_voice(voice).code
                if code != UNKNOWN_CODE:
                    codes.add(code)
        except (ProviderError, OSError):
            continue
    return codes


def modes_for(work: WorkRef, settings: Settings) -> list[ReadMode]:
    """The modes this work supports, in reading order. Source is always there."""
    modes = [ReadMode.SOURCE, ReadMode.BRIEF]
    if work.language in spoken_languages(settings):
        modes.append(ReadMode.LISTEN)
    return modes


def default_voice(settings: Settings, language: str) -> str:
    """The first voice a configured provider offers for `language`, or `""`."""
    from videomaker.providers import get_provider, resolve_chain
    from videomaker.providers.tts.kokoro_onnx import describe_voice

    for name in resolve_chain("tts", settings):
        try:
            provider = get_provider("tts", name, settings)
            if isinstance(provider, HTTPTTSProvider):
                if language in provider.cfg.languages and provider.cfg.voices:
                    return provider.cfg.voices[0]
                continue
            assert isinstance(provider, TTSProvider)
            for voice in sorted(provider.voices()):
                if describe_voice(voice).code == language:
                    return voice
        except (ProviderError, OSError):
            continue
    return ""


# ---------------------------------------------------------------------- loading


def _corpus(request: Request) -> CorpusProvider:
    deps = _deps(request)
    corpus = deps.provider("corpus")
    if not isinstance(corpus, CorpusProvider):  # pragma: no cover - registry guarantees it
        raise HTTPException(status_code=500, detail="the configured corpus is not a corpus")
    return corpus


def _deps(request: Request):
    return reader_deps(request.app.state.settings)


def _work(request: Request, work_id: str) -> WorkRef:
    for work in _corpus(request).works():
        if work.id == work_id:
            return work
    raise HTTPException(status_code=404, detail="no such work")


def _ref(work_id: str, book: str, chapter: str) -> UnitRef:
    """Validate the URL segments before anything touches the filesystem.

    `book` is checked against `BOOK_ORDER` rather than against the directory
    listing: the canon is a fixed table, so a hostile segment is refused without a
    single `stat`. `chapter` is taken as a string and parsed here rather than
    declared `int`, because FastAPI's own coercion answers a non-numeric segment
    with a 422 carrying the offending value back — a reader who mistyped a URL
    should get the same plain 404 as one who asked for a chapter that is not there.
    """
    if book.upper() not in BOOK_ORDER or not chapter.isdigit() or int(chapter) < 1:
        raise HTTPException(status_code=404, detail="no such chapter")
    return UnitRef(work_id=work_id, book=book.upper(), chapter=int(chapter))


def _unit(request: Request, ref: UnitRef) -> UnitText:
    try:
        return _corpus(request).unit(ref)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="no such chapter") from exc


# ------------------------------------------------------------------- the outline


def _chapters_by_book(refs: list[UnitRef]) -> list[dict[str, object]]:
    """`outline` is already in canonical order, so one pass groups it."""
    books: list[dict[str, object]] = []
    for ref in refs:
        if not books or books[-1]["code"] != ref.book:
            books.append({"code": ref.book, "name": book_name(ref.book), "chapters": []})
        books[-1]["chapters"].append(ref.chapter)
    return books


def neighbours(refs: list[UnitRef], ref: UnitRef) -> tuple[UnitRef | None, UnitRef | None]:
    """The chapter before and after `ref` in the work, across book boundaries."""
    keys = [candidate.key() for candidate in refs]
    try:
        index = keys.index(ref.key())
    except ValueError:
        return None, None
    return (refs[index - 1] if index > 0 else None, refs[index + 1] if index + 1 < len(refs) else None)


# ------------------------------------------------------------------------ pages


@router.get("/library")
def library_page(request: Request):
    """Every imported work, each with the licence it was imported under."""
    templates: Jinja2Templates = request.app.state.templates
    settings: Settings = request.app.state.settings
    works = _corpus(request).works()
    return templates.TemplateResponse(
        request,
        "library.html",
        {
            "works": [
                {"work": work, "modes": [m.value for m in modes_for(work, settings)]}
                for work in works
            ]
        },
    )


@router.get("/library/{work_id}")
def work_page(request: Request, work_id: str):
    work = _work(request, work_id)
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "work.html",
        {"work": work, "books": _chapters_by_book(_corpus(request).outline(work_id))},
    )


def _mode_context(request: Request, ref: UnitRef, mode: ReadMode, voice: str) -> dict[str, object]:
    """Everything one mode's fragment needs, and nothing another mode would cost.

    `BRIEF` is the only mode that can call a model, and it does so only when it is
    the mode being asked for — opening a chapter in `SOURCE` must not quietly spend
    a request on a brief nobody looked at.
    """
    unit = _unit(request, ref)
    context: dict[str, object] = {"unit": unit, "ref": ref, "mode": mode.value, "voice": voice}
    if mode is ReadMode.BRIEF:
        deps = _deps(request)
        try:
            context["brief"] = build_brief(unit, deps)
        except ProviderError as exc:
            context["brief"] = None
            context["brief_error"] = str(exc)
    if mode is ReadMode.LISTEN:
        context.update(_listen_context(request, unit, voice))
    return context


def _reading_root(settings: Settings, ref: UnitRef, key: str) -> Path:
    return (
        library_dir(settings.workspace_dir)
        / ref.work_id
        / DERIVED_DIRNAME
        / AUDIO_DIRNAME
        / key
    )


def _listen_context(request: Request, unit: UnitText, voice: str) -> dict[str, object]:
    """The player when the reading exists, the estimate and a button when it does not."""
    settings: Settings = request.app.state.settings
    deps = _deps(request)
    jobs: JobQueue = request.app.state.jobs
    job = jobs.state_for(job_key(unit.ref, voice))
    context: dict[str, object] = {
        "job": job,
        "poll": job is not None and job.state in {"queued", "running"},
        "job_key": job_key(unit.ref, voice),
        "reading": None,
        "estimate_minutes": estimate_minutes(unit.plain),
        "needs_confirmation": False,
        "cost_usd": 0.0,
    }
    try:
        key = reading_key(unit, provider=deps.leading_name("tts"), voice=voice, speed=1.0)
    except ProviderError:
        return context
    finished = _reading_root(settings, unit.ref, key) / "reading.json"
    if finished.is_file():
        reading = Reading.model_validate_json(finished.read_text())
        context["reading"] = reading
        context["media_base"] = f"/media/reading/{unit.ref.work_id}/{key}"
        return context
    # A paid vendor is asked *before* the button is drawn, so the page can show the
    # estimate and a confirm rather than an error after the fact.
    try:
        provider = deps.provider("tts")
    except ProviderError:
        return context
    if isinstance(provider, HTTPTTSProvider) and provider.cfg.cost_per_minute_usd > 0:
        minutes = context["estimate_minutes"]
        context["cost_usd"] = minutes * provider.cfg.cost_per_minute_usd
        try:
            from videomaker.providers.tts.http_api import TTSBudget, check_budget

            check_budget(minutes, cfg=provider.cfg, budget=TTSBudget())
        except TTSBudgetExceeded:
            context["needs_confirmation"] = True
    return context


@router.get("/read/{work_id}/{book}/{chapter}")
def read_page(request: Request, work_id: str, book: str, chapter: str, mode: str = ""):
    work = _work(request, work_id)
    settings: Settings = request.app.state.settings
    ref = _ref(work_id, book, chapter)
    modes = modes_for(work, settings)
    chosen = ReadMode(mode) if mode in {m.value for m in modes} else modes[0]
    voice = default_voice(settings, work.language)
    outline = _corpus(request).outline(work_id)
    previous, following = neighbours(outline, ref)
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "read.html",
        {
            "work": work,
            "modes": [m.value for m in modes],
            "previous": previous,
            "next_ref": following,
            **_mode_context(request, ref, chosen, voice),
        },
    )


@router.get("/read/{work_id}/{book}/{chapter}/mode/{mode}")
def read_mode(request: Request, work_id: str, book: str, chapter: str, mode: str):
    """One mode, as an htmx fragment. Unknown or unsupported modes are a 404."""
    work = _work(request, work_id)
    settings: Settings = request.app.state.settings
    modes = modes_for(work, settings)
    if mode not in {m.value for m in modes}:
        raise HTTPException(status_code=404, detail="this work does not support that mode")
    ref = _ref(work_id, book, chapter)
    templates: Jinja2Templates = request.app.state.templates
    context = _mode_context(
        request, ref, ReadMode(mode), default_voice(settings, work.language)
    )
    return templates.TemplateResponse(
        request, "_reader_mode.html", {"work": work, "modes": [m.value for m in modes], **context}
    )


@router.post("/read/{work_id}/{book}/{chapter}/audio")
def build_audio(
    request: Request,
    work_id: str,
    book: str,
    chapter: str,
    confirmed: str = Form(""),
):
    """Enqueue the synthesis and return the polling fragment. Never synthesises here."""
    work = _work(request, work_id)
    settings: Settings = request.app.state.settings
    if ReadMode.LISTEN not in modes_for(work, settings):
        raise HTTPException(status_code=404, detail="this work has no voice")
    ref = _ref(work_id, book, chapter)
    # 404 a chapter that is not there before a job is queued for it.
    _unit(request, ref)
    voice = default_voice(settings, work.language)
    jobs: JobQueue = request.app.state.jobs
    jobs.submit(job_key(ref, voice), READING_JOB_KIND, _reading_job(settings, ref, voice, bool(confirmed)))
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "_reader_mode.html",
        {
            "work": work,
            "modes": [m.value for m in modes_for(work, settings)],
            **_mode_context(request, ref, ReadMode.LISTEN, voice),
        },
    )


def _reading_job(settings: Settings, ref: UnitRef, voice: str, confirmed: bool):
    """The job body. Built here so the worker thread holds no request state."""

    def job(progress) -> None:
        deps = reader_deps(settings)
        corpus = deps.provider("corpus")
        assert isinstance(corpus, CorpusProvider)
        progress.update(stage="voice", progress=0.1, message=f"synthesising {ref.key()}")
        build_reading(corpus.unit(ref), deps, voice=voice, confirmed=confirmed)
        progress.update(stage="voice", progress=1.0, message="ready")

    return job


# ------------------------------------------------------------------- the media


@router.get("/media/reading/{work_id}/{reading_key}/{name}")
def get_reading_media(request: Request, work_id: str, reading_key: str, name: str):
    """Serve one reading artefact, through `web/media.py`'s guard and nothing else.

    `reading_key` is a hash in normal use and an arbitrary string in a hostile one,
    so it is resolved inside the work's `derived/audio/` and proved to be contained
    there. Every rejection is the same 404: distinguishing "outside" from "absent"
    would turn this into a probe for the filesystem.
    """
    if name not in SERVABLE:
        raise HTTPException(status_code=404, detail="not found")
    settings: Settings = request.app.state.settings
    try:
        work_root = media.project_root(library_dir(settings.workspace_dir), work_id)
        audio_root = work_root / DERIVED_DIRNAME / AUDIO_DIRNAME
        target = media.safe_project_path(audio_root, f"{reading_key}/{name}")
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail="not found") from exc
    return FileResponse(target, media_type=SERVABLE[name])


#: Imported for the doctor and the tests, which describe the library by name.
__all__ = [
    "LIBRARY_DIRNAME",
    "Brief",
    "ReadMode",
    "modes_for",
    "neighbours",
    "router",
    "spoken_languages",
]
