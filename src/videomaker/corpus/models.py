"""What a work, a reference and a passage are, on disk and in memory.

The whole reader hangs off `UnitRef`: it is the cache key, the URL path, the
directory name under `units/`, and the thing a reader types. So it is a model with
a total, reversible string form rather than a tuple passed around — `key()` and
`parse()` are inverses, and a test asserts it for both shapes.

**`WorkRef.licence` is `min_length=1` and that is load-bearing.** It is where
`docs/multimodal-reader-design.md` §5's rule — ship nothing whose licence you
cannot state in one sentence — stops being prose and starts being a
`ValidationError`. ebible.org has no site-wide licence and each translation's own
page is the authority, so there is no honest default to fall back on and none is
offered.
"""

import re
from enum import StrEnum

from pydantic import BaseModel, Field

#: USFM/Paratext book codes are three characters of `[0-9A-Z]` — `GEN`, `1CO`,
#: `JHN`. The pattern is enforced rather than documented because the code is
#: interpolated into a directory name (`units/<BOOK>/`) and into a URL path, and a
#: reference is reader-supplied at both. Refusing the shape here means neither the
#: importer nor the web layer has to think about `..`.
_BOOK_CODE = r"[0-9A-Z]{3}"

#: Chapters are zero-padded to three digits in `key()` so a directory listing and a
#: sorted list of keys agree with the reader's idea of order. 150 (Psalms) is the
#: largest in the Protestant canon, so three digits is enough and four would only
#: make every key longer.
CHAPTER_DIGITS = 3

_KEY = re.compile(
    rf"^(?P<work_id>[a-z0-9][a-z0-9_-]*)/(?P<book>{_BOOK_CODE})/(?P<chapter>\d+)"
    r"(?:\.(?P<first>\d+)-(?P<last>\d+))?$"
)


class Versification(StrEnum):
    """Which chapter-and-verse scheme a work counts by.

    Recorded per work rather than assumed, because the same passage is numbered
    differently between them — the Psalms shift by one for most of the Psalter
    between `KJV` and `VULG`/`LXX`, and `ORG` (the Hebrew original) counts a
    superscription as verse 1. Nothing in this MVP converts between them; storing
    the scheme is what makes a converter possible later without re-importing.
    """

    KJV = "kjv"
    VULG = "vulg"
    LXX = "lxx"
    ORG = "org"


class Verse(BaseModel):
    """One numbered verse of source text. Never a heading, never a footnote."""

    number: int = Field(ge=1)
    text: str = Field(min_length=1)


class WorkRef(BaseModel):
    """One text in the library, and the provenance that lets it be redistributed."""

    id: str
    title: str
    #: ISO 639-1.
    #
    # TODO(owner): the plan's Interfaces block says this "must be a key of
    # `languages.LANGUAGES`", but that dict holds only the nine languages Kokoro
    # has voices for, and `docs/multimodal-reader-design.md` §9.1 sequences Swahili
    # (`sw`) as the next import — a text whose language has no local voice, which
    # §6 says must still be readable. Enforcing membership would make that import
    # impossible, so the field is left as the plain `str` the block specifies.
    # Should `languages.LANGUAGES` grow text-only entries (`offered=False`, and a
    # note saying no voice rather than a broken one), or should the constraint be
    # dropped?
    language: str
    #: One sentence. See the module docstring: this is the licence gate.
    licence: str = Field(min_length=1)
    licence_url: str
    source_url: str
    versification: Versification = Versification.KJV
    #: Whether a reading server shows this work at all.
    #:
    #: Optional with a falsy default, so every `work.yaml` already on disk loads
    #: unchanged — the same compatibility rule `Project`'s newer fields follow.
    #: The default is deliberately the private one: importing a text is not the
    #: same act as putting it in front of other people, and the safe direction for
    #: a flag nobody has set yet is "not yet".
    published: bool = False


class UnitRef(BaseModel):
    """A passage's address: a work, a book, a chapter, and optionally a verse range."""

    work_id: str
    #: USFM/Paratext three-letter code, uppercase: `GEN`, `JHN`, `REV`.
    book: str = Field(pattern=rf"^{_BOOK_CODE}$")
    chapter: int = Field(ge=1)
    #: `None` means the whole chapter, which is what the reader almost always wants.
    verses: tuple[int, int] | None = None

    def key(self) -> str:
        """`"web/JHN/003"`, or `"web/JHN/003.16-18"` for a range.

        The inverse of `parse`, and the only string form there is: the same value
        addresses a cache directory, a URL and a line in a log.
        """
        chapter = f"{self.chapter:0{CHAPTER_DIGITS}d}"
        if self.verses is None:
            return f"{self.work_id}/{self.book}/{chapter}"
        first, last = self.verses
        return f"{self.work_id}/{self.book}/{chapter}.{first}-{last}"

    @classmethod
    def parse(cls, key: str) -> "UnitRef":
        """Read back a `key()`. Raises `ValueError` naming the input on anything else."""
        match = _KEY.match(key)
        if match is None:
            raise ValueError(f"not a unit key: {key!r}")
        first, last = match["first"], match["last"]
        return cls(
            work_id=match["work_id"],
            book=match["book"],
            chapter=int(match["chapter"]),
            verses=None if first is None else (int(first), int(last)),
        )


class UnitText(BaseModel):
    """A passage, versified. The one thing every reader mode is derived from."""

    ref: UnitRef
    #: How a human writes it: "John 3".
    title: str
    verses: list[Verse]

    @property
    def plain(self) -> str:
        """The passage as continuous prose, with the verse numbers gone.

        This is what a narrator is given and what a brief is written from, and the
        numbers are dropped because a narrator that reads them aloud is reading
        something that is not in the text. `corpus/audio.py` keeps the numbers by
        synthesising verse by verse instead.
        """
        return " ".join(verse.text for verse in self.verses)

    @property
    def word_count(self) -> int:
        return len(self.plain.split())
