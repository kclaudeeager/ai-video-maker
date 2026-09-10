"""Fetching licence-clear music into the user's own library.

**The project still ships no audio.** This writes into `assets/music/<mood>/`,
which `.gitignore` excludes, so nothing fetched can be committed even by
accident — `/CLAUDE.md` rule 3 holds by construction rather than by care. What
this adds is the tedious half: finding a track whose licence permits the use,
downloading it, and writing the credit line down *at download time*, which
`assets/music/library.yaml` says is the thing nobody manages to reconstruct six
months later.

**The licence gate is the point, and it is stricter than "free".** A track is
refused unless it is CC0, Public Domain Mark, CC BY or CC BY-SA:

* **NonCommercial is refused** because the videos this tool makes are meant to be
  monetised, and NC is precisely the term that forbids it;
* **NoDerivatives is refused** because a music bed mixed under narration and
  ducked against it is an adaptation, whatever one calls it in conversation.

That is the same argument `docs/audio-design.md` makes for shipping nothing: a
Content ID claim on a monetised video is the outcome this project exists to
avoid, and the cheapest way to never cause one is to never accept a file we
cannot state the terms of. There is no `--force`, for the same reason the text
importer has none.

**Extracting audio from a YouTube watch URL is not a source and never will be.**
`docs/audio-design.md` rejects it outright: it violates YouTube's terms and the
underlying music is virtually always somebody's copyright. The YouTube *Audio
Library* is fine and has no public API, so it stays a manual download.

Sources are described in `config.yaml` under `music_sources:`, the same shape and
for the same reason as `voice_providers:` — a second catalogue should be a YAML
entry rather than a patch. The default is Openverse, which aggregates
Freesound, Jamendo, ccMixter and Wikimedia, states a licence per item, needs no
API key, and hands back a ready-made attribution string.
"""

import re
from pathlib import Path
from typing import Any

import httpx
import yaml
from pydantic import BaseModel, Field

from videomaker.audio import LIBRARY_FILENAME
from videomaker.config import Settings
from videomaker.downloads import download_file

#: Licences a track may carry to be accepted, as the short codes Openverse uses.
#: Everything else — every `nc`, every `nd`, and anything unrecognised — is
#: refused. See the module docstring for why these two exclusions in particular.
ALLOWED_LICENCES: frozenset[str] = frozenset({"cc0", "pdm", "by", "by-sa"})

#: Why each refusal happened, so the message can say more than "no".
_REFUSALS: dict[str, str] = {
    "nc": "NonCommercial: it forbids the monetised use this tool is for",
    "nd": "NoDerivatives: a bed mixed under narration and ducked is an adaptation",
    "sampling+": "a sampling licence does not cover using the track as a bed",
}

#: Audio containers worth accepting. Anything else is either video or a format
#: FFmpeg would have to be talked into, and neither belongs in a music library.
AUDIO_FILETYPES: frozenset[str] = frozenset({"mp3", "ogg", "oga", "flac", "wav", "m4a"})

DEFAULT_LIMIT = 8
#: Milliseconds. A bed under a two-minute video wants more than a stab and less
#: than a symphony; outside this the track is almost always a one-shot effect or
#: a DJ set, and neither is what `music_mood` means.
MIN_DURATION_MS = 20_000
MAX_DURATION_MS = 900_000

_NON_SLUG = re.compile(r"[^a-z0-9]+")


class MusicSource(BaseModel):
    """How to ask one catalogue for tracks, and where its answers keep things.

    Dotted paths rather than code so a second catalogue is a `music_sources:`
    entry — the argument `providers/tts/http_api.HTTPTTSConfig` already makes,
    and the reason no catalogue's name appears in this module except as a default.
    """

    name: str
    endpoint: str
    query_param: str = "q"
    #: Sent with every request. The default asks Openverse for the licences this
    #: module will accept, so the refusal below is a second line of defence
    #: rather than the only one.
    params: dict[str, str] = Field(default_factory=lambda: {"license": "cc0,pdm,by,by-sa"})
    page_size_param: str = "page_size"
    results_path: str = "results"
    #: Where each field of a `Candidate` lives inside one result.
    title_path: str = "title"
    artist_path: str = "creator"
    licence_path: str = "license"
    licence_url_path: str = "license_url"
    attribution_path: str = "attribution"
    source_url_path: str = "foreign_landing_url"
    download_path: str = "url"
    filetype_path: str = "filetype"
    duration_path: str = "duration"
    timeout_s: float = 30.0


#: The one blessed catalogue, as a pointer and nothing else — the same shape
#: `corpus/catalogue.py` uses for texts.
OPENVERSE = MusicSource(name="openverse", endpoint="https://api.openverse.org/v1/audio/")


class Candidate(BaseModel):
    """One track a source offered, before anything has been downloaded."""

    title: str
    artist: str = ""
    licence: str
    licence_url: str = ""
    #: The exact credit line, as the source states it. `library.yaml` prefers this
    #: over anything composed from the other fields, because some licences dictate
    #: the wording and a paraphrase is not compliance.
    attribution: str
    source_url: str
    download_url: str
    filetype: str = "mp3"
    duration_ms: int = 0

    @property
    def duration_s(self) -> float:
        return self.duration_ms / 1000

    def filename(self) -> str:
        stem = _NON_SLUG.sub("-", self.title.casefold()).strip("-") or "track"
        return f"{stem[:60].strip('-')}.{self.filetype}"


class Refused(ValueError):
    """A track this library will not accept, and the sentence saying why."""


def _walk(payload: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        if isinstance(payload, list) and part.isdigit():
            payload = payload[int(part)]
        elif isinstance(payload, dict):
            payload = payload.get(part)
        else:
            return None
    return payload


def check_licence(licence: str) -> None:
    """Raise `Refused` unless this licence permits a monetised, mixed-under use."""
    code = (licence or "").strip().lower()
    if not code:
        raise Refused("no licence stated, so there is nothing to stand behind")
    if code in ALLOWED_LICENCES:
        return
    for marker, reason in _REFUSALS.items():
        if marker in code:
            raise Refused(f"{code} is {reason}")
    allowed = ", ".join(sorted(ALLOWED_LICENCES))
    raise Refused(f"{code} is not one this library accepts ({allowed})")


def candidate_from(result: dict, source: MusicSource) -> Candidate | None:
    """One search result to a `Candidate`, or None if it cannot be one.

    A result missing a licence, an attribution or a landing page is dropped here
    rather than offered and refused later: the gate is about what can be
    *credited*, and a row that cannot be is not a candidate at all.
    """
    licence = str(_walk(result, source.licence_path) or "")
    attribution = str(_walk(result, source.attribution_path) or "")
    source_url = str(_walk(result, source.source_url_path) or "")
    download_url = str(_walk(result, source.download_path) or "")
    if not (licence and attribution and source_url and download_url):
        return None
    filetype = str(_walk(result, source.filetype_path) or "mp3").lower()
    if filetype not in AUDIO_FILETYPES:
        return None
    try:
        check_licence(licence)
    except Refused:
        return None
    duration = int(_walk(result, source.duration_path) or 0)
    if duration and not MIN_DURATION_MS <= duration <= MAX_DURATION_MS:
        return None
    return Candidate(
        title=str(_walk(result, source.title_path) or "untitled"),
        artist=str(_walk(result, source.artist_path) or ""),
        licence=licence,
        licence_url=str(_walk(result, source.licence_url_path) or ""),
        attribution=attribution,
        source_url=source_url,
        download_url=download_url,
        filetype=filetype,
        duration_ms=duration,
    )


def configured_sources(settings: Settings) -> list[MusicSource]:
    """Every `music_sources:` entry, or the default when none is configured."""
    configured = [MusicSource.model_validate(entry) for entry in settings.music_sources]
    return configured or [OPENVERSE]


def search(
    query: str,
    *,
    settings: Settings,
    limit: int = DEFAULT_LIMIT,
    client: httpx.Client | None = None,
) -> list[Candidate]:
    """Ask every configured source, keeping only what this library would accept."""
    found: list[Candidate] = []
    for source in configured_sources(settings):
        own = client is None
        http = client or httpx.Client(timeout=source.timeout_s, follow_redirects=True)
        try:
            response = http.get(
                source.endpoint,
                params={
                    source.query_param: query,
                    source.page_size_param: str(max(limit * 2, limit)),
                    **source.params,
                },
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            results = _walk(response.json(), source.results_path) or []
        finally:
            if own:
                http.close()
        for result in results:
            if not isinstance(result, dict):
                continue
            candidate = candidate_from(result, source)
            if candidate is not None:
                found.append(candidate)
            if len(found) >= limit:
                return found
    return found


# ------------------------------------------------------------------ the library


def relpath_for(mood: str, candidate: Candidate) -> str:
    """`music/<mood>/<file>` — the key `library.yaml` records a track under."""
    return f"music/{mood}/{candidate.filename()}"


def fetch(
    candidate: Candidate,
    *,
    mood: str,
    settings: Settings,
    client: httpx.Client | None = None,
) -> Path:
    """Download one accepted track and write its credit line down.

    The licence is checked again here rather than trusted from the search: `fetch`
    is callable on a `Candidate` from anywhere, and a gate that only guards one
    path is not a gate.
    """
    check_licence(candidate.licence)
    destination = Path(settings.music_dir) / mood / candidate.filename()
    destination.parent.mkdir(parents=True, exist_ok=True)
    download_file(candidate.download_url, destination, client=client)
    record(candidate, relpath_for(mood, candidate), settings)
    return destination


def record(candidate: Candidate, relpath: str, settings: Settings) -> None:
    """Add this track to `library.yaml`, keeping every comment above the data.

    The file ships as ~40 lines of documentation and no entries, and it is the one
    file in the audio tree that *is* committed. A round trip through PyYAML would
    silently delete all of that, so the header is preserved verbatim and only the
    `tracks:` block is regenerated.
    """
    path = Path(settings.music_dir) / LIBRARY_FILENAME
    header, tracks = _split_library(path)
    tracks[relpath] = {
        "title": candidate.title,
        "artist": candidate.artist,
        "licence": candidate.licence,
        "attribution": candidate.attribution,
        "source_url": candidate.source_url,
    }
    body = yaml.safe_dump({"tracks": tracks}, sort_keys=True, allow_unicode=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{header.rstrip()}\n\n{body}" if header.strip() else body)


def _split_library(path: Path) -> tuple[str, dict[str, dict[str, str]]]:
    """Everything above `tracks:`, and the entries under it."""
    try:
        text = path.read_text()
    except OSError:
        return "", {}
    try:
        loaded = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        loaded = {}
    tracks = loaded.get("tracks") if isinstance(loaded, dict) else None
    entries = {
        str(key): {str(k): str(v) for k, v in value.items()}
        for key, value in (tracks or {}).items()
        if isinstance(value, dict)
    }
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("tracks:"):
            return "\n".join(lines[:index]), entries
    return text, entries
