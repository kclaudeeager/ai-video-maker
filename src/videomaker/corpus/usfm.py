"""USFM in, versified text out — with everything that is not the text left behind.

A narrator must not read a footnote marker aloud, and a brief must not summarise a
section heading the translators wrote. So the parse keeps exactly two kinds of
node — chapter and verse markers, and the running text between them — and drops
headings, footnotes, cross-references, and the character-marker *wrappers* (`\\nd
...\\nd*` keeps its word and loses its tags). `source/` keeps the original file
untouched for anyone who wants the apparatus back.

**Why `usfm-grammar` rather than a regex.** Footnotes nest character markers,
cross-references sit mid-verse, a verse can span a paragraph break, and a verse
bridge (`\\v 1-2`) is a number range. A regex that handles all of that is a parser
written badly; `usfm-grammar` is the reference tree-sitter grammar (MIT). It is
imported lazily so its 0.18 s first import is paid by `videomaker library import`
and by nothing else.

**Grammar errors are tolerated, deliberately.** Measured on the World English
Bible archive (2026-09-10): the grammar reports 42 errors in Psalms and 9 in
Habakkuk, every one of them a `\\qs Selah\\+w ...\\+w*\\qs*` nest, plus 7 in the
front-matter file. With `ignore_errors=True` the tree is still built and the
verse text around each error survives intact — "Selah." is kept, and so is the
word inside the `\\nd` next to it — so a strict parse would have refused the
whole of Psalms to protect it from nothing. A file with no chapter (front matter,
a glossary) yields no units and is skipped by having nothing to write.
"""

from collections.abc import Iterator
from typing import Any

from videomaker.corpus.models import UnitRef, UnitText, Verse
from videomaker.corpus.refs import book_name

#: `parse_usfm` fills the reference's work with this; the importer knows the work
#: and stamps the real id. The file itself never says which translation it is.
#
# TODO(owner): the plan's `parse_usfm(text) -> UnitText | None` has nowhere to
# take a `work_id` from, and `UnitRef.work_id` is required. Is a placeholder plus
# a stamp in `import_work` the intended shape, or should the signature grow a
# `work_id` keyword?
UNKNOWN_WORK = ""


def _usj_content(text: str) -> list[Any]:
    from usfm_grammar import Filter, USFMParser

    parser = USFMParser(text)
    usj = parser.to_usj(include_markers=Filter.BCV + Filter.TEXT, ignore_errors=True)
    return usj["content"]


def _verse_number(raw: str) -> int:
    """`"12"` -> 12; a bridge `"12-13"` -> 12, the first verse it covers.

    A bridge is one run of text the translators would not split; filing it under
    its first number keeps the text and keeps the numbering monotonic. The verses it
    swallows are absent rather than empty, which `Verse.text` (`min_length=1`)
    would refuse anyway.
    """
    return int(raw.split("-", 1)[0].split(",", 1)[0])


def iter_chapters(text: str) -> Iterator[UnitText]:
    """Every chapter of one USFM book that holds at least one verse, in file order.

    Chapters with no verse text — the `\\c` of a front-matter file, or a chapter
    whose every verse is empty — are skipped rather than emitted empty: an outline
    that lists a chapter a reader cannot open is worse than a shorter outline.
    """
    book: str | None = None
    chapter: int | None = None
    verses: list[Verse] = []
    number: int | None = None
    pieces: list[str] = []

    def flush_verse() -> None:
        nonlocal number, pieces
        joined = " ".join(" ".join(pieces).split())
        if number is not None and joined:
            verses.append(Verse(number=number, text=joined))
        number, pieces = None, []

    def flush_chapter() -> UnitText | None:
        nonlocal verses
        flush_verse()
        if book is None or chapter is None or not verses:
            verses = []
            return None
        ref = UnitRef(work_id=UNKNOWN_WORK, book=book, chapter=chapter)
        unit = UnitText(ref=ref, title=f"{book_name(book)} {chapter}", verses=verses)
        verses = []
        return unit

    for node in _usj_content(text):
        if isinstance(node, str):
            if number is not None:
                pieces.append(node)
            continue
        kind = node.get("type")
        if kind == "book":
            book = str(node.get("code", "")).upper()
        elif kind == "chapter":
            unit = flush_chapter()
            if unit is not None:
                yield unit
            chapter = int(node["number"])
        elif kind == "verse":
            flush_verse()
            number = _verse_number(str(node["number"]))
    unit = flush_chapter()
    if unit is not None:
        yield unit


def parse_usfm(text: str) -> UnitText | None:
    """One chapter of USFM — `\\id` line included — to its `UnitText`.

    `None` when the text holds no verse. Given a whole book, this raises rather
    than returning the first chapter: a silent partial parse would import one
    chapter of Psalms and call it done. Use `iter_chapters` for a book file.
    """
    units = list(iter_chapters(text))
    if not units:
        return None
    if len(units) > 1:
        raise ValueError(f"parse_usfm reads one chapter; this text has {len(units)}")
    return units[0]
