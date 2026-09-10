"""A whole work as one zip: the text, whatever has been narrated, and the licence.

The feed (`corpus/feed.py`) is the subscription answer and it is the better one
for anyone who has a podcast player. This is the other half: a bundle needs no
player, no subscription and no network after the download, and it is what
survives this server being switched off. It is also the answer to "can I have
what I made" that does not require explaining RSS to anybody.

**Entries are numbered.** `001-GEN-001.mp3` rather than `GEN-001.mp3`, because
file managers and media players sort alphabetically and `EXO` sorts before `GEN`
— which would play Exodus before Genesis, exactly the reordering that
`itunes:type=serial` exists to prevent in the feed. The number is the canonical
position, so the order is stated in the filenames rather than hoped for.

**The licence travels with it.** A redistributable text stops being
redistributable the moment it is separated from the sentence that says so, and a
zip on somebody's laptop is precisely that separation. `importer.ImportSpec`
refuses a work whose licence cannot be stated in one sentence; this is where that
sentence has to leave the tool alongside the words it covers.
"""

import zipfile
from pathlib import Path

from videomaker.config import Settings
from videomaker.corpus.audio import AUDIO_DIRNAME as READINGS_DIRNAME
from videomaker.corpus.audio import MP3_FILE, VTT_FILE
from videomaker.corpus.feed import Episode, episodes_for
from videomaker.corpus.importer import DERIVED_DIRNAME, work_dir
from videomaker.corpus.models import UnitRef, WorkRef
from videomaker.providers.base import CorpusProvider

#: The point at which a download stops being a convenience. A fully narrated
#: Bible is ~1,189 chapters at the ~1.4 MB per chapter measured on the WEB
#: readings, so about 1.6 GB — a zip nobody wants to start over a phone
#: connection, and one that takes a minute of CPU to assemble before a single
#: byte reaches the browser. Text alone is nowhere near it: the whole WEB is
#: ~4.5 MB.
MAX_BUNDLE_BYTES = 500 * 1024 * 1024

#: Where the text of a chapter goes inside the archive, and where its audio goes.
#: Two directories rather than one, so a listener can drag the audio folder into
#: a player without the .txt files landing in a playlist.
TEXT_DIRNAME = "text"
AUDIO_DIRNAME = "audio"

LICENCE_FILE = "LICENCE.txt"
README_FILE = "README.txt"


class BundleTooLarge(Exception):
    """The archive would exceed `MAX_BUNDLE_BYTES`.

    Raised rather than silently packing a prefix: a bundle missing half a book,
    with nothing in it saying so, is worse than an error naming the size.
    """


def bundle_name(work: WorkRef) -> str:
    """The filename a browser should save this as."""
    return f"{work.id}.zip"


def _stem(position: int, ref: UnitRef) -> str:
    """`001-GEN-001` — canonical position, book, chapter.

    The position is padded to four digits because a work can have more than 999
    chapters (the Protestant canon has 1,189) and a 1,000th entry that sorted
    before the 999th would defeat the whole point of numbering them.
    """
    return f"{position:04d}-{ref.book}-{ref.chapter:03d}"


def _audio_dir(settings: Settings, work_id: str, key: str) -> Path:
    return work_dir(settings.workspace_dir, work_id) / DERIVED_DIRNAME / READINGS_DIRNAME / key


def _chapter_text(title: str, verses: list) -> str:
    """The passage as a plain text file, verse numbers kept.

    Numbered rather than `UnitText.plain`, which is what a narrator is given: a
    reader who takes a chapter offline is reading it, and a verse number is how
    anybody refers to a line of it afterwards.
    """
    lines = [title, "=" * len(title), ""]
    lines.extend(f"{verse.number}  {verse.text}" for verse in verses)
    return "\n".join(lines) + "\n"


def _readme(work: WorkRef, *, chapters: int, narrated: int) -> str:
    audio = f"{narrated} of them read aloud" if narrated else "no audio yet"
    return (
        f"{work.title}\n"
        f"{'=' * len(work.title)}\n\n"
        f"{chapters} chapters, {audio}.\n\n"
        f"{TEXT_DIRNAME}/   one .txt per chapter, verse numbers kept\n"
        f"{AUDIO_DIRNAME}/  one .mp3 per narrated chapter, with its .vtt captions\n\n"
        "Files are numbered in reading order, because players and file managers\n"
        "sort alphabetically and would otherwise start you in the wrong place.\n\n"
        f"Licence: {work.licence}\n"
        f"         {work.licence_url}\n"
        f"Source:  {work.source_url}\n\n"
        "Made with Longhand.\n"
    )


def bundle_size(settings: Settings, work_id: str, *, corpus: CorpusProvider) -> int:
    """Roughly what the archive will weigh, without building it.

    The audio decides it — mp3s are stored uncompressed and the text is a
    rounding error beside them — so this counts the audio exactly and ignores
    the rest. Called before a single byte is written, so an oversized work is
    refused up front rather than after a minute of zipping.
    """
    return sum(episode.bytes for episode in episodes_for(settings, work_id, corpus=corpus))


def write_bundle(
    path: Path,
    work: WorkRef,
    *,
    settings: Settings,
    corpus: CorpusProvider,
) -> None:
    """Write the whole work to `path` as a zip.

    Text for every chapter, audio for the chapters that have it. A work nobody
    has narrated still bundles: that is a book to read on a plane, and gating the
    download on narration would leave the free-tier deploy — which cannot narrate
    at all — with nothing to offer.
    """
    if bundle_size(settings, work.id, corpus=corpus) > MAX_BUNDLE_BYTES:
        raise BundleTooLarge(
            f"{work.title} is larger than {MAX_BUNDLE_BYTES // (1024 * 1024)} MB packaged"
        )

    outline = corpus.outline(work.id)
    narrated: dict[str, Episode] = {
        episode.ref.key(): episode for episode in episodes_for(settings, work.id, corpus=corpus)
    }

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for position, ref in enumerate(outline, start=1):
            stem = _stem(position, ref)
            unit = corpus.unit(ref)
            archive.writestr(
                f"{TEXT_DIRNAME}/{stem}.txt", _chapter_text(unit.title, unit.verses)
            )

            episode = narrated.get(ref.key())
            if episode is None:
                continue
            source = _audio_dir(settings, work.id, episode.key)
            # Stored, not deflated: an mp3 is already compressed, so deflating it
            # spends CPU on every byte of the largest files in the archive to save
            # under a percent. The text above keeps the default, where it earns it.
            archive.write(
                source / MP3_FILE,
                f"{AUDIO_DIRNAME}/{stem}.mp3",
                compress_type=zipfile.ZIP_STORED,
            )
            captions = source / VTT_FILE
            if captions.is_file():
                archive.write(captions, f"{AUDIO_DIRNAME}/{stem}.vtt")

        archive.writestr(LICENCE_FILE, f"{work.licence}\n{work.licence_url}\n")
        archive.writestr(
            README_FILE, _readme(work, chapters=len(outline), narrated=len(narrated))
        )
