"""A narrated work as a podcast feed.

A work whose chapters have been read aloud *is* a podcast, and every player on
earth already speaks RSS. That makes this the cheapest publishing mechanism
available to a local-first tool: no account, no service, no new dependency — the
owner sends somebody a URL and their existing app does the rest.

**It is a `serial`, and that is not decoration.** A podcast client sorts newest
first by default, which for a book is backwards: nobody wants Revelation before
Genesis. `itunes:type=serial` together with `itunes:episode` is the standard way
to say "start at one and go forward", so the ordering is stated in the format
rather than fought with pubDates.

**The XML is built here rather than in a Jinja template, deliberately.** RSS
enclosures must be absolute — a player has no base URL to resolve against — and
`/CLAUDE.md` rule 6 forbids an absolute URL in a template, enforced by
`tests/unit/test_web_templates.py`. That rule is about never fetching from a CDN
and never announcing a page view to a third party; an enclosure pointing at this
very server is neither of those. Generating outside the template layer keeps the
rule exactly as strict as it was, and gets correct escaping from the standard
library for free.
"""

import json
from email.utils import formatdate
from xml.etree import ElementTree as ET

from pydantic import BaseModel

from videomaker.config import Settings
from videomaker.corpus.audio import AUDIO_DIRNAME, MP3_FILE, READING_FILE
from videomaker.corpus.importer import DERIVED_DIRNAME, work_dir
from videomaker.corpus.models import UnitRef, WorkRef
from videomaker.providers.base import CorpusProvider

#: How many chapters a feed carries. Podcast feeds are conventionally capped —
#: a client re-downloads the whole document on every refresh — and a fully
#: narrated Bible would otherwise be 1,189 items and a file read each. The first
#: 300 in canonical order is what a serial listener is working through.
MAX_EPISODES = 300

ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"

#: Registered so ElementTree writes `itunes:duration` rather than inventing an
#: `ns0:` prefix — which is legal XML and which some podcast clients ignore.
ET.register_namespace("itunes", ITUNES)


class Episode(BaseModel):
    """One narrated chapter, as a feed sees it."""

    ref: UnitRef
    title: str
    #: The reading key, which is also the segment in the media path.
    key: str
    duration_s: float
    bytes: int
    #: When the reading was made, for `pubDate`.
    made_at: float

    @property
    def media_path(self) -> str:
        return f"/media/reading/{self.ref.work_id}/{self.key}/{MP3_FILE}"

    @property
    def page_path(self) -> str:
        return f"/read/{self.ref.work_id}/{self.ref.book}/{self.ref.chapter}?mode=listen"


def _readings(settings: Settings, work_id: str) -> dict[str, tuple[str, dict, float, int]]:
    """The newest finished reading of each chapter: `unit_key -> (key, data, mtime, bytes)`.

    A chapter narrated in two voices is two directories and one episode — a
    listener asked for the chapter, not for every take of it — so the newest wins.
    """
    root = work_dir(settings.workspace_dir, work_id) / DERIVED_DIRNAME / AUDIO_DIRNAME
    best: dict[str, tuple[str, dict, float, int]] = {}
    if not root.is_dir():
        return best
    for directory in root.iterdir():
        finished = directory / READING_FILE
        audio = directory / MP3_FILE
        if not finished.is_file() or not audio.is_file():
            continue
        try:
            data = json.loads(finished.read_text())
            unit_key = UnitRef.model_validate(data["ref"]).key()
        except (OSError, ValueError, KeyError):
            continue
        made_at = finished.stat().st_mtime
        if unit_key not in best or made_at > best[unit_key][2]:
            best[unit_key] = (directory.name, data, made_at, audio.stat().st_size)
    return best


def episodes_for(settings: Settings, work_id: str, *, corpus: CorpusProvider) -> list[Episode]:
    """Every narrated chapter of this work, in canonical reading order.

    Driven by `corpus.outline` rather than by the directory listing, so the order
    is the canon's and not the filesystem's — the same reason `BibleCorpus`
    sorts its outline at all.
    """
    made = _readings(settings, work_id)
    if not made:
        return []
    episodes: list[Episode] = []
    for ref in corpus.outline(work_id):
        found = made.get(ref.key())
        if found is None:
            continue
        key, data, made_at, size = found
        try:
            title = corpus.unit(ref).title
        except KeyError:  # pragma: no cover - outline and units cannot disagree
            continue
        episodes.append(
            Episode(
                ref=ref,
                title=title,
                key=key,
                duration_s=float(data.get("duration_s") or 0.0),
                bytes=size,
                made_at=made_at,
            )
        )
        if len(episodes) >= MAX_EPISODES:
            break
    return episodes


def _hms(seconds: float) -> str:
    whole = max(int(seconds), 0)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:d}:{minutes:02d}:{secs:02d}"


def feed_xml(work: WorkRef, episodes: list[Episode], *, base_url: str) -> str:
    """The channel, as a podcast client expects it.

    `base_url` comes from the request, so a feed works on whatever address the
    listener actually reached — localhost, a LAN address, a tunnel — rather than
    on one baked into a config file that will be wrong the first time it moves.
    """
    base = base_url.rstrip("/")
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = work.title
    ET.SubElement(channel, "link").text = f"{base}/library/{work.id}"
    ET.SubElement(channel, "description").text = (
        f"{work.title}, read aloud. The narration is the source text, "
        f"chapter by chapter. {work.licence}"
    )
    ET.SubElement(channel, "language").text = work.language
    ET.SubElement(channel, "generator").text = "Longhand"
    ET.SubElement(channel, f"{{{ITUNES}}}author").text = "Longhand"
    # A book is meant to be started at the beginning; `serial` is how the format
    # says so, and it is why the episodes below are numbered.
    ET.SubElement(channel, f"{{{ITUNES}}}type").text = "serial"
    ET.SubElement(channel, f"{{{ITUNES}}}explicit").text = "false"
    ET.SubElement(channel, f"{{{ITUNES}}}summary").text = work.licence

    for number, episode in enumerate(episodes, start=1):
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = episode.title
        ET.SubElement(item, "link").text = f"{base}{episode.page_path}"
        ET.SubElement(item, "description").text = f"{episode.title}, read aloud."
        # Not a permalink: the reading key moves when the voice changes, and the
        # thing that does not move is which chapter this is.
        guid = ET.SubElement(item, "guid", {"isPermaLink": "false"})
        guid.text = episode.ref.key()
        ET.SubElement(item, "pubDate").text = formatdate(episode.made_at, usegmt=True)
        ET.SubElement(
            item,
            "enclosure",
            {
                "url": f"{base}{episode.media_path}",
                "length": str(episode.bytes),
                "type": "audio/mpeg",
            },
        )
        ET.SubElement(item, f"{{{ITUNES}}}duration").text = _hms(episode.duration_s)
        ET.SubElement(item, f"{{{ITUNES}}}episode").text = str(number)

    ET.indent(rss, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(rss, encoding="unicode")
