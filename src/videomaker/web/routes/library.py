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
* **The reader's preference is a signed cookie, not a setting.** `Settings` is
  machine-wide; which mode a person prefers is per person, and two readers on one
  self-hosted instance must not overwrite each other. It is signed rather than
  encrypted because there is nothing secret in it — the signature is only there so
  a tampered value falls back to the default instead of reaching `ReadMode`.

* **Building audio goes through the `JobQueue`,** like every other long job: the
  POST returns a polling fragment immediately. A chapter of forty verses is forty
  synthesis calls and a concat, which is not a request cycle.
"""

import hashlib
import hmac
import secrets
from base64 import urlsafe_b64decode, urlsafe_b64encode
from enum import StrEnum
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ValidationError

from videomaker.config import Audience, Settings
from videomaker.corpus.audio import (
    AUDIO_DIRNAME,
    Reading,
    build_reading,
    reader_deps,
    reading_key,
)
from videomaker.corpus.digest import Brief, build_brief, build_brief_within_budget
from videomaker.corpus.documents import DOCUMENT_BOOK
from videomaker.corpus.importer import DERIVED_DIRNAME, LIBRARY_DIRNAME, library_dir
from videomaker.corpus.models import UnitRef, UnitText, WorkRef
from videomaker.corpus.refs import BOOK_ORDER, book_label
from videomaker.providers.base import CorpusProvider, TTSProvider
from videomaker.providers.errors import ProviderError, QuotaExceeded
from videomaker.providers.tts.http_api import HTTPTTSProvider, TTSBudgetExceeded, estimate_minutes
from videomaker.web import media
from videomaker.web.auth import PASSWORD_ENV, USERNAME, is_loopback
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

#: Every book code a URL may name: the canon, plus the code documents are filed
#: under. Fixed, so validating a path segment costs no filesystem access.
READABLE_BOOKS: frozenset[str] = frozenset(BOOK_ORDER) | {DOCUMENT_BOOK}


class ReadMode(StrEnum):
    SOURCE = "source"
    BRIEF = "brief"
    LISTEN = "listen"
    #: Not a fourth way of reading: a way of *leaving* the reader. It materialises
    #: an ordinary project from the passage and hands you to gate 1, because a
    #: reader asking to watch a chapter is a creator starting a project. The three
    #: gates are not bypassed and no fourth is added.
    WATCH = "watch"


#: What each mode is called on the switcher. Here rather than in the template
#: because a Jinja lookup miss renders as an empty string: `watch` was added to
#: `ReadMode` and not to the literal, and the tab drew as a blank gap that no test
#: was looking at. `test_every_mode_has_a_label` closes that.
MODE_LABELS: dict[str, str] = {
    ReadMode.SOURCE: "Read",
    ReadMode.BRIEF: "Brief",
    ReadMode.LISTEN: "Listen",
    ReadMode.WATCH: "Watch",
}

#: What a reader who has expressed no preference gets. The brief is the way in:
#: it is the mode that says what the chapter is about before asking anyone to read
#: it, and the source text is one control away from it.
DEFAULT_MODE = ReadMode.BRIEF

PREFS_COOKIE = "longhand_reader"
#: A year. The cookie holds a mode, a language and where you were up to; there is
#: nothing in it that needs to expire, and a preference that resets every session
#: is not one.
PREFS_MAX_AGE_S = 365 * 24 * 3600

#: How many works keep a place. Newest first, so the ninth pushes the oldest out.
#: Eight because the cookie has to stay small — a browser drops one over 4 KB, and
#: losing every preference to remember a ninth book is a bad trade.
MAX_BOOKMARKS = 8

#: Used when `Settings.reader_cookie_secret` is empty: a per-process key, so
#: preferences survive as long as the server does and no longer.
_PROCESS_SECRET = secrets.token_urlsafe(32)


class Bookmark(BaseModel):
    """Where a reader stopped in one work.

    Per work rather than one global place: someone reading two books at once
    should not have them fight over a single slot. Kept in the same signed cookie
    as the mode, because it is the same kind of fact — per person, not per
    machine — and `Settings` is machine-wide.
    """

    work_id: str
    unit_key: str
    title: str
    #: The verse the narration had reached, or `0` for the top of the unit.
    verse: int = 0

    @property
    def href(self) -> str:
        ref = UnitRef.parse(self.unit_key)
        anchor = f"#v{self.verse}" if self.verse else ""
        return f"/read/{ref.work_id}/{ref.book}/{ref.chapter}{anchor}"


def _secret(settings: Settings) -> bytes:
    return (settings.reader_cookie_secret or _PROCESS_SECRET).encode()


def _sign(payload: str, settings: Settings) -> str:
    digest = hmac.new(_secret(settings), payload.encode(), hashlib.sha256).digest()
    return urlsafe_b64encode(digest).decode().rstrip("=")


def encode_prefs(
    settings: Settings,
    *,
    mode: str,
    language: str = "",
    places: list[Bookmark] | None = None,
) -> str:
    """`<mode>|<language>|<places>.<signature>` — small, readable, tamper-evident.

    The places are `work/key/verse/title` joined by `~`, which no field of a
    bookmark can contain: a work id is a slug, a unit key is a slug and digits,
    and a title is escaped by the same `|`-free rule as the rest of the payload.
    A title carrying one would be silently truncated rather than corrupting the
    cookie, which is why `decode_prefs` rebuilds each bookmark by position.
    """
    rows = "~".join(_encode_place(place) for place in (places or []))
    payload = urlsafe_b64encode(f"{mode}|{language}|{rows}".encode()).decode().rstrip("=")
    return f"{payload}.{_sign(payload, settings)}"


def _encode_place(place: Bookmark) -> str:
    """`<unit_key>:<verse>:<title>`.

    `:` is the field separator because no other field can contain one — a unit
    key is a slug, a book code and digits — and the title has both separators
    stripped rather than escaped. A title is a label; losing a colon from one is
    invisible, and an escaping scheme here would be three lines of code guarding
    a cookie nobody can read anyway.
    """
    title = place.title.replace(":", " ").replace("~", " ")
    return f"{place.unit_key}:{place.verse}:{title}"


def _decode_place(row: str) -> Bookmark | None:
    """One row back to a bookmark, or None for anything that is not one.

    Every failure is the same answer, for the same reason the mode's is: a lost
    place is not worth an error page, and a cookie written by an older build must
    be harmless rather than fatal.
    """
    unit_key, _, rest = row.partition(":")
    verse, _, title = rest.partition(":")
    try:
        ref = UnitRef.parse(unit_key)
        return Bookmark(
            work_id=ref.work_id, unit_key=unit_key, title=title or unit_key, verse=int(verse)
        )
    except (ValueError, ValidationError):
        return None


def decode_prefs(settings: Settings, raw: str) -> tuple[ReadMode, str]:
    """The reader's preference, or the default for anything that is not one.

    Every failure lands on the same answer: no cookie, a truncated one, a forged
    signature, an unknown mode. A preference is not worth an error page, and
    falling back is what keeps a stale cookie from a previous version harmless.
    """
    payload, _, signature = (raw or "").partition(".")
    if not payload or not signature:
        return DEFAULT_MODE, ""
    if not hmac.compare_digest(signature, _sign(payload, settings)):
        return DEFAULT_MODE, ""
    try:
        decoded = urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode()
    except (ValueError, UnicodeDecodeError):
        return DEFAULT_MODE, ""
    mode, _, rest = decoded.partition("|")
    if mode not in {m.value for m in ReadMode}:
        return DEFAULT_MODE, ""
    language, _, _places = rest.partition("|")
    return ReadMode(mode), language


def decode_places(settings: Settings, raw: str) -> list[Bookmark]:
    """Where this reader was up to, newest first. `[]` for anything unreadable."""
    payload, _, signature = (raw or "").partition(".")
    if not payload or not signature:
        return []
    if not hmac.compare_digest(signature, _sign(payload, settings)):
        return []
    try:
        decoded = urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode()
    except (ValueError, UnicodeDecodeError):
        return []
    rows = decoded.split("|", 2)[2] if decoded.count("|") >= 2 else ""
    places = [_decode_place(row) for row in rows.split("~") if row]
    return [place for place in places if place is not None][:MAX_BOOKMARKS]


def _first_supported(modes: list[ReadMode], *wanted: str) -> ReadMode:
    """The first of `wanted` this work supports, else its first supported mode."""
    supported = {m.value for m in modes}
    for candidate in wanted:
        if candidate in supported:
            return ReadMode(candidate)
    return modes[0]


def preferred(request: Request) -> tuple[ReadMode, str]:
    return decode_prefs(request.app.state.settings, request.cookies.get(PREFS_COOKIE, ""))


def bookmarks(request: Request) -> list[Bookmark]:
    return decode_places(request.app.state.settings, request.cookies.get(PREFS_COOKIE, ""))


def bookmark_for(request: Request, work_id: str) -> Bookmark | None:
    return next((place for place in bookmarks(request) if place.work_id == work_id), None)


def with_place(places: list[Bookmark], place: Bookmark) -> list[Bookmark]:
    """`place` at the front, one entry per work, oldest dropped past the cap."""
    kept = [other for other in places if other.work_id != place.work_id]
    return [place, *kept][:MAX_BOOKMARKS]


def remember(
    response,
    settings: Settings,
    *,
    mode: ReadMode,
    language: str,
    places: list[Bookmark] | None = None,
) -> None:
    """Write the preference and the places back. `httponly`: no script reads it."""
    response.set_cookie(
        PREFS_COOKIE,
        encode_prefs(settings, mode=mode.value, language=language, places=places),
        max_age=PREFS_MAX_AGE_S,
        httponly=True,
        samesite="lax",
    )


def job_key(ref: UnitRef, voice: str) -> str:
    """One job per chapter *and voice*: two voices are two different readings."""
    return f"{JOB_PREFIX}{ref.key()}:{voice}"


# ------------------------------------------------------------------ what is possible


def spoken_languages(settings: Settings) -> set[str]:
    """ISO codes a configured `tts` provider can actually speak.

    A YAML vendor states its languages outright and is taken at its word: a vendor
    exists precisely to reach a language the local stack cannot, so filtering its
    claim through `languages.py` — which measures Kokoro and espeak — would refuse
    the Kinyarwanda that `docs/voice-providers.md` is written for.

    A local provider does not state them, so its voice ids are read through the
    table `web/voices.py` uses: the voice prefix *is* the language (M3 Task 21).
    Its codes are then narrowed to `OFFERED_CODES`, the measured gate, because
    Kokoro **has** voices for languages this stack was measured to speak badly —
    through espeak its Japanese "runs four times too long and says the English
    word 'Japanese' out loud", and its Mandarin loses tone (`languages.py`). A
    Listen tab that produces that is exactly the "present and broken" the mode
    table exists to avoid.

    The font gate is deliberately *not* applied. `media.fonts.offerable_codes`
    adds it because burned-in captions have to be drawn by libass; the reader
    renders HTML and the browser draws it, so a script this machine has no caption
    font for is still perfectly readable here.

    A provider that cannot be built at all contributes nothing rather than failing
    the page: an unconfigured voice is a missing mode, not an error.

    TODO(owner): `hi` is held back by `OFFERED_CODES` for an *alignment* reason —
    faster-whisper returns the take in Urdu script — and the reader does no
    alignment at all, so Hindi narration may well be fine here. Narrowing to
    `OFFERED_CODES` drops it along with `ja` and `zh`, which is the conservative
    call rather than a measured one. Should `languages.py` distinguish "the audio
    is wrong" from "the timing is wrong", so the reader can offer the second?
    """
    from videomaker.languages import OFFERED_CODES
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
                if code != UNKNOWN_CODE and code in OFFERED_CODES:
                    codes.add(code)
        except (ProviderError, OSError):
            continue
    return codes


def rendered_for(store, ref: UnitRef) -> tuple[object, str] | None:
    """The finished wide cut made from this passage, if there is one.

    `(project, relpath)`, or None. Used to decide whether a reader is offered
    anything to watch at all — they are never offered an empty player.
    """
    from videomaker.corpus.materialise import existing_for
    from videomaker.models import Aspect

    project = existing_for(store, ref)
    if project is None:
        return None
    spec = project.outputs.get(Aspect.WIDE)
    if spec is None or not spec.video_path:
        return None
    return project, spec.video_path


def modes_for(
    work: WorkRef, settings: Settings, *, ref: UnitRef | None = None, store=None
) -> list[ReadMode]:
    """The modes this work supports, in reading order. Source is always there.

    **Two different things share the word "watch".** Commissioning a video
    materialises a `Project`, which needs the three gates and a studio to approve
    them in; playing one that already exists needs neither. A studio is offered
    both, under one tab. A reader is offered the second and never the first — and
    only when there *is* something, because an empty player is worse than no tab.

    `ref` and `store` are optional because the library shelf asks about a *work*
    and cannot know which chapter; with no `ref` a reader is offered no `WATCH`.
    """
    modes = [ReadMode.SOURCE, ReadMode.BRIEF]
    if work.language in spoken_languages(settings):
        modes.append(ReadMode.LISTEN)
    if may_watch(settings, ref=ref, store=store):
        modes.append(ReadMode.WATCH)
    return modes


def may_watch(settings: Settings, *, ref: UnitRef | None, store) -> bool:
    """Whether this audience is offered the Watch tab for this passage.

    A studio always: it may commission as well as play, and the panel offers
    both. A reader only where a render already exists — an empty player is worse
    than no tab, and a reader is never shown a control that would start work.

    One function rather than a branch in `modes_for` because the two answers are
    the same `append` and a linter is right to collapse that; the difference is
    the *reason*, and a reason belongs somewhere it can be read.
    """
    if settings.audience is not Audience.READER:
        return True
    if ref is None or store is None:
        return False
    return rendered_for(store, ref) is not None


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


def visible_works(request: Request) -> list[WorkRef]:
    """The works this audience may see.

    **The corpus is a library, not a policy**: `BibleCorpus.works()` returns
    everything imported and the filtering happens here, where the audience is
    known. A studio sees the lot; a reading server sees what its owner published.
    """
    works = _corpus(request).works()
    if request.app.state.settings.audience is not Audience.READER:
        return works
    return [work for work in works if work.published]


def _work(request: Request, work_id: str) -> WorkRef:
    """The work, or a 404.

    An unpublished work is a 404 on a reading server rather than a hidden row or
    a 403: a reader must not be able to tell a private work from one that was
    never imported. Every reader route goes through here — the outline, the
    chapters, the modes, the audio and the video — so there is one place to be
    right rather than six.
    """
    for work in visible_works(request):
        if work.id == work_id:
            return work
    raise HTTPException(status_code=404, detail="no such work")


def _ref(work_id: str, book: str, chapter: str) -> UnitRef:
    """Validate the URL segments before anything touches the filesystem.

    `book` is checked against `READABLE_BOOKS` rather than against the directory
    listing: it is a fixed table, so a hostile segment is refused without a single
    `stat`. The table is the 66 canonical codes plus the one a document of your
    own is filed under — a code deliberately outside every canon, so admitting it
    widens what can be read without widening what can be reached. `chapter` is taken as a string and parsed here rather than
    declared `int`, because FastAPI's own coercion answers a non-numeric segment
    with a 422 carrying the offending value back — a reader who mistyped a URL
    should get the same plain 404 as one who asked for a chapter that is not there.
    """
    if book.upper() not in READABLE_BOOKS or not chapter.isdigit() or int(chapter) < 1:
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
            books.append({"code": ref.book, "name": book_label(ref.book), "chapters": []})
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
    works = visible_works(request)
    return templates.TemplateResponse(
        request,
        "library.html",
        {
            "works": [
                {
                    "work": work,
                    "modes": [m.value for m in modes_for(work, settings)],
                    "place": bookmark_for(request, work.id),
                }
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
        {
            "work": work,
            "books": _chapters_by_book(_corpus(request).outline(work_id)),
            # Absolute, because a podcast player is given this URL with no page to
            # resolve it against — and built here rather than in the template so
            # rule 6's "no absolute URL in a template" stays exactly as strict as
            # it was. Taken from the request, so whatever address reached this page
            # is the address handed to the player: localhost, a LAN IP, a tunnel.
            "feed_url": f"{str(request.base_url).rstrip('/')}/library/{work_id}/feed.xml",
            # `localhost` is precisely the one address no *other* device can reach:
            # on a phone it means the phone. Since `serve` binds loopback by
            # default, the common case is a feed URL that works only in a player on
            # this machine — so say that, and say what to run instead, rather than
            # promising "any device" and being wrong. Not fixed by printing the LAN
            # address here: bound to loopback, that address would not answer either.
            "feed_is_local_only": is_loopback(request.url.hostname or ""),
            "public_bind_command": "videomaker serve --host 0.0.0.0",
            "password_env": PASSWORD_ENV,
            # A network bind is gated by `PasswordGate`, and the gate covers the
            # feed and the mp3s alike — a player handed a bare URL gets 401 and
            # most report it as "feed not found". Read off this very request
            # rather than the environment: if the browser had to authenticate to
            # see this page, a player will have to authenticate too, and that
            # stays true behind a tunnel or a proxy that the server cannot see.
            "feed_needs_password": "authorization" in request.headers,
            "auth_username": USERNAME,
        },
    )


def _mode_context(request: Request, ref: UnitRef, mode: ReadMode, voice: str) -> dict[str, object]:
    """Everything one mode's fragment needs, and nothing another mode would cost.

    `BRIEF` is the only mode that can call a model, and it does so only when it is
    the mode being asked for — opening a chapter in `SOURCE` must not quietly spend
    a request on a brief nobody looked at.
    """
    unit = _unit(request, ref)
    context: dict[str, object] = {
        "MODE_LABELS": MODE_LABELS,
        "reader_view": request.app.state.settings.audience is Audience.READER,
        "unit": unit,
        "ref": ref,
        "mode": mode.value,
        "voice": voice,
        # A verse number is part of a versified text and a reader looks for it.
        # A paragraph number is something this importer made up, and printed as a
        # superscript it reads as a footnote marker on a document that has none.
        "numbered": ref.book != DOCUMENT_BOOK,
    }
    if mode is ReadMode.BRIEF:
        deps = _deps(request)
        # A visitor's brief is counted; the owner's is not. Writing one is an LLM
        # call triggered by a GET, so on a reading server it is the one thing a
        # stranger can spend. `QuotaExceeded` is a `ProviderError`, so the
        # existing "there is no brief yet" path catches it — over budget is a
        # quieter day rather than a failure.
        write = (
            build_brief_within_budget
            if request.app.state.settings.audience is Audience.READER
            else build_brief
        )
        try:
            context["brief"] = write(unit, deps)
        except ProviderError as exc:
            context["brief"] = None
            context["brief_error"] = str(exc)
        # Which model wrote it is a fact about the workshop, not about the
        # passage. The retelling label stays either way: that is the feature.
        context["show_model"] = request.app.state.settings.audience is not Audience.READER
    if mode is ReadMode.LISTEN:
        context.update(_listen_context(request, unit, voice))
        if request.app.state.settings.audience is Audience.READER:
            _ask_for_narration(request, unit, voice, context)
    if mode is ReadMode.WATCH:
        context.update(_watch_context(request, ref))
    return context


def _watch_context(request: Request, ref: UnitRef) -> dict[str, object]:
    """What the Watch panel shows: the project for this passage, if there is one.

    Nothing is created by *looking*. The panel is a description of what watching
    would mean and a button that means it — materialising on a GET would leave a
    project behind every time somebody clicked the wrong tab.
    """
    from videomaker.corpus.materialise import existing_for

    store = request.app.state.store
    made = rendered_for(store, ref)
    if made is not None:
        project, relpath = made
        # A narrow URL rather than `/media/<project>/<path>`: on a reading server
        # the project media route is not mounted at all, because it would serve
        # every script, take and `project.json` in the workspace to anybody.
        return {
            "project": project,
            "rendered": relpath,
            "watch_url": f"/media/watch/{ref.work_id}/{ref.book}/{ref.chapter}",
        }
    return {"project": existing_for(store, ref), "rendered": None, "watch_url": ""}


class AtCapacity(RuntimeError):
    """A visitor asked for more narration than this server will make today."""


def request_narration(
    request: Request, unit: UnitText, voice: str, *, confirmed: bool = False
) -> str:
    """Enqueue the narration of `unit`, counting what a visitor spends. Returns the job key.

    **One place, because there are two ways in**: the auto-request when a reader
    opens Listen, and `POST .../audio`, which a reading server still routes even
    though its template offers no button. A guard on the path with a button is not
    a guard.

    Three things are free and must stay free:

    * a reading already on disk — re-hearing a chapter costs nothing, and a
      visitor is never refused something the machine has already made;
    * a request already in flight — the panel polls itself, and a poll that
      booked minutes would charge a visitor once a second for waiting;
    * everything, in a studio. The owner's machine is theirs to grind.

    Raises `AtCapacity` when a visitor is over the cap, having enqueued nothing.
    """
    from videomaker.corpus.audio import (
        READER_NARRATION_KEY,
        narration_budget,
        narration_minutes,
    )

    jobs: JobQueue = request.app.state.jobs
    settings: Settings = request.app.state.settings
    key = job_key(unit.ref, voice)

    state = jobs.state_for(key)
    if state is not None and state.state in {"queued", "running"}:
        return key

    if settings.audience is Audience.READER:
        deps = _deps(request)
        minutes = narration_minutes(unit)
        try:
            deps.quota.check(READER_NARRATION_KEY, narration_budget(settings))
        except QuotaExceeded as exc:
            raise AtCapacity(str(exc)) from exc
        jobs.submit(key, READING_JOB_KIND, _reading_job(settings, unit.ref, voice, confirmed))
        # Booked after the submit, so a queue that refuses the job charges nobody.
        deps.quota.record(READER_NARRATION_KEY, minutes)
        deps.quota.save()
        return key

    jobs.submit(key, READING_JOB_KIND, _reading_job(settings, unit.ref, voice, confirmed))
    return key


def _ask_for_narration(
    request: Request, unit: UnitText, voice: str, context: dict[str, object]
) -> None:
    """Start the narration on sight, on a reading server.

    A button whose only answer is yes is a question nobody needed asking, so a
    reader who opens Listen gets the narration rather than an invitation to
    request it. `JobQueue.submit` is a no-op for a key already in flight, so
    opening the tab twice — or the poll re-rendering this fragment every 1.5 s —
    enqueues exactly one job.

    **It refuses rather than asks where a paid voice is over budget.** The studio
    shows the estimate and a confirm button because the person looking at it owns
    the account. A reader does not, so they are told the narration is not
    available here instead of being invited to spend somebody else's money.
    """
    if context.get("reading") is not None or context.get("poll"):
        return
    if context.get("needs_confirmation"):
        context["unavailable"] = True
        return
    try:
        key = request_narration(request, unit, voice)
    except AtCapacity:
        # Not an error and not the same as the paid refusal: this one changes by
        # waiting, so the page says so and the passage is right there.
        context["at_capacity"] = True
        return
    state = request.app.state.jobs.state_for(key)
    context["job"] = state
    context["poll"] = state is not None and state.state in {"queued", "running"}


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


@router.get("/library/{work_id}/feed.xml")
def work_feed(request: Request, work_id: str):
    """This work's narrated chapters, as a podcast feed.

    Goes through `_work`, so it respects `published` exactly as every other reader
    route does and an unpublished work has no feed on a reading server.

    Generates nothing: it lists the readings already on disk, so it needs no
    budget and cannot be used to make a server work. It is also built per request
    rather than written to a file, which is why it can never be stale.
    """
    from videomaker.corpus.feed import episodes_for, feed_xml

    work = _work(request, work_id)
    episodes = episodes_for(
        request.app.state.settings, work_id, corpus=_corpus(request)
    )
    xml = feed_xml(work, episodes, base_url=str(request.base_url))
    return Response(content=xml, media_type="application/rss+xml")


@router.get("/read/{work_id}/{book}/{chapter}")
def read_page(request: Request, work_id: str, book: str, chapter: str, mode: str = ""):
    work = _work(request, work_id)
    settings: Settings = request.app.state.settings
    ref = _ref(work_id, book, chapter)
    modes = modes_for(work, settings, ref=ref, store=request.app.state.store)
    remembered, _language = preferred(request)
    # An explicit `?mode=` first, then the cookie, then the default, then whatever
    # this work does support — so a reader whose preference is Listen can still
    # open a work that has no voice, and lands on the brief rather than on
    # whichever mode happens to sort first.
    chosen = _first_supported(modes, mode, remembered.value, DEFAULT_MODE.value)
    voice = default_voice(settings, work.language)
    outline = _corpus(request).outline(work_id)
    previous, following = neighbours(outline, ref)
    templates: Jinja2Templates = request.app.state.templates
    context = _mode_context(request, ref, chosen, voice)
    unit: UnitText = context["unit"]  # type: ignore[assignment]
    response = templates.TemplateResponse(
        request,
        "read.html",
        {
            "work": work,
            "modes": [m.value for m in modes],
            "previous": previous,
            "next_ref": following,
            **context,
        },
    )
    # Opening a unit *is* the bookmark: there is no "save my place" control, and
    # a reader who has to press one has already lost their place once.
    place = Bookmark(work_id=work.id, unit_key=ref.key(), title=unit.title)
    remember(
        response,
        settings,
        mode=chosen,
        language=work.language,
        places=with_place(bookmarks(request), place),
    )
    return response


@router.get("/read/{work_id}/{book}/{chapter}/mode/{mode}")
def read_mode(request: Request, work_id: str, book: str, chapter: str, mode: str):
    """One mode, as an htmx fragment. Unknown or unsupported modes are a 404."""
    work = _work(request, work_id)
    settings: Settings = request.app.state.settings
    ref = _ref(work_id, book, chapter)
    modes = modes_for(work, settings, ref=ref, store=request.app.state.store)
    if mode not in {m.value for m in modes}:
        raise HTTPException(status_code=404, detail="this work does not support that mode")
    templates: Jinja2Templates = request.app.state.templates
    context = _mode_context(
        request, ref, ReadMode(mode), default_voice(settings, work.language)
    )
    response = templates.TemplateResponse(
        request, "_reader_mode.html", {"work": work, "modes": [m.value for m in modes], **context}
    )
    # Switching mode *is* the preference: there is no separate control to set it,
    # which is the only reason a cookie is worth having here at all. The places
    # ride along unchanged — writing the cookie without them would erase every
    # bookmark the moment somebody pressed Brief.
    remember(
        response,
        settings,
        mode=ReadMode(mode),
        language=work.language,
        places=bookmarks(request),
    )
    return response


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
    voice = default_voice(settings, work.language)
    unit = _unit(request, ref)
    try:
        request_narration(request, unit, voice, confirmed=bool(confirmed))
    except AtCapacity:
        pass  # The panel below says so; `_listen_context` will find no job.
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "_reader_mode.html",
        {
            "work": work,
            "modes": [m.value for m in modes_for(work, settings, ref=ref, store=request.app.state.store)],
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


@router.post("/read/{work_id}/{book}/{chapter}/watch")
def watch(request: Request, work_id: str, book: str, chapter: str):
    """Materialise this passage as a project and hand the reader to gate 1.

    A 303 to the project's own page, which is where the script is read and
    approved — the same screen a project started from a topic arrives at. Pressing
    it twice lands on the same project: the unit key is the identity.
    """
    from videomaker.corpus.materialise import materialise

    _work(request, work_id)
    ref = _ref(work_id, book, chapter)
    unit = _unit(request, ref)
    store = request.app.state.store
    project = materialise(ref, unit=unit, store=store)
    jobs: JobQueue = request.app.state.jobs
    if not project.scenes:
        from videomaker.web.routes.projects import script_job

        jobs.submit(project.id, "run", script_job(request.app.state.settings, project.id))
    return RedirectResponse(url=f"/projects/{project.id}", status_code=303)


@router.post("/read/{work_id}/{book}/{chapter}/place")
def record_place(
    request: Request, work_id: str, book: str, chapter: str, verse: int = Form(0)
):
    """Record how far the narration got, so resuming resumes rather than restarts.

    Posted by `reader.js` as the verse changes, and answered with `204 No
    Content`: nothing on the page changes, and re-rendering a fragment forty
    times a chapter to update a cookie would be absurd.
    """
    work = _work(request, work_id)
    ref = _ref(work_id, book, chapter)
    unit = _unit(request, ref)
    settings: Settings = request.app.state.settings
    mode, _language = preferred(request)
    place = Bookmark(
        work_id=work.id, unit_key=ref.key(), title=unit.title, verse=max(verse, 0)
    )
    response = Response(status_code=204)
    remember(
        response,
        settings,
        mode=mode,
        language=work.language,
        places=with_place(bookmarks(request), place),
    )
    return response


@router.get("/media/watch/{work_id}/{book}/{chapter}")
def get_watch_media(request: Request, work_id: str, book: str, chapter: str):
    """Serve the rendered wide cut made from this passage, and nothing else.

    `media.router` serves any file in any project directory, which is right for a
    studio and wrong for a reading server: it would hand a visitor every script,
    every take and every `project.json`. So a reading server does not mount it,
    and this is how a finished video is reached instead — one artefact, resolved
    from the passage rather than from a path the caller supplies, and still put
    through the same containment guard as everything else.
    """
    _work(request, work_id)
    ref = _ref(work_id, book, chapter)
    store = request.app.state.store
    made = rendered_for(store, ref)
    if made is None:
        raise HTTPException(status_code=404, detail="not found")
    project, relpath = made
    try:
        root = media.project_root(store.projects_dir, project.id)
        target = media.safe_project_path(root, relpath)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail="not found") from exc
    return FileResponse(target, media_type="video/mp4")


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
    "DEFAULT_MODE",
    "LIBRARY_DIRNAME",
    "PREFS_COOKIE",
    "Bookmark",
    "Brief",
    "ReadMode",
    "bookmark_for",
    "bookmarks",
    "decode_places",
    "decode_prefs",
    "encode_prefs",
    "modes_for",
    "neighbours",
    "preferred",
    "router",
    "spoken_languages",
]
