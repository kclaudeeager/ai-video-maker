"""Your own document, read as a work: plain text, Markdown, EPUB.

**This is the task that proves the reader generalises past scripture.** A work is
chapters of numbered units; a document is sections of paragraphs; those are the
same shape, so once a file is in that shape the reader — source, brief, narration,
verse highlighting, the lot — works on it unchanged. Nothing downstream of here
knows the difference.

The mapping, and it is the whole design:

* a **chapter** is a heading — `#`/`##` in Markdown, a spine item in an EPUB, a
  blank-line-fenced short line in plain text;
* a **verse** is a paragraph, numbered from 1 within its chapter.

Paragraph-as-verse is what makes the audio work: `corpus/audio.py` synthesises one
file per unit and times the reading from each file's own duration, so paragraphs
give a document exactly the same click-to-seek and current-unit highlighting a
chapter of John gets, for no extra code.

**The licence gate still applies, and is still honest.** Your own file is not
redistributed by this project and the recorded licence says exactly that. It is
stated rather than blank, because a blank one is what the gate exists to refuse,
and there is still no `--force` (`/CLAUDE.md`, rule 4).

EPUB is a zip of XHTML, so it is `zipfile` plus `html.parser` — both standard
library. **No new dependency**, which is why EPUB is in and PDF is not: PDF text
extraction needs one, and a PDF's reading order is a research problem rather than
a parse.
"""

import html.parser
import re
import unicodedata
import zipfile
from pathlib import Path

from pydantic import BaseModel, Field

from videomaker.corpus.importer import (
    DERIVED_DIRNAME,
    SOURCE_DIRNAME,
    UNITS_DIRNAME,
    WORK_FILE,
    unit_path,
    work_dir,
)
from videomaker.corpus.models import UnitRef, UnitText, Verse, WorkRef
from videomaker.project import slugify

#: What can be read without a new dependency. PDF is deliberately absent — see
#: the module docstring.
DOCUMENT_SUFFIXES: frozenset[str] = frozenset({".txt", ".md", ".markdown", ".epub"})

#: Documents have no canon, so they get a USFM-shaped code of their own. `DOC` is
#: not a book of any Bible, which is exactly why it is safe: `UnitRef.book` is
#: three uppercase characters and the web layer validates against `BOOK_ORDER`,
#: so a document work is addressable without ever colliding with scripture.
DOCUMENT_BOOK = "DOC"

#: One licence sentence, and it is the truth about a file you uploaded.
OWN_FILE_LICENCE = (
    "Your own document: held in your workspace and redistributed by nobody."
)
OWN_FILE_URL = "about:your-own-file"

#: A plain-text line is treated as a heading when it is short, alone between blank
#: lines, and not sentence-shaped. Measured against the README and a handful of
#: novels from Project Gutenberg: 60 characters admits every real chapter title
#: seen and rejects every first line of prose.
_PLAIN_HEADING_MAX_CHARS = 60

#: Markdown constructs that are not the words. Fenced code is dropped whole: a
#: narrator reading a shell transcript aloud is not reading the document.
_FENCE = re.compile(r"^```")
_MD_HEADING = re.compile(r"^(#{1,3})\s+(.*)$")
_MD_INLINE = re.compile(r"[*_`]{1,3}")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_MD_BULLET = re.compile(r"^\s{0,3}(?:[-*+]|\d+[.)])\s+")
_MD_QUOTE = re.compile(r"^\s{0,3}>\s?")

_WHITESPACE = re.compile(r"\s+")


class DocumentSpec(BaseModel):
    work_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    title: str = Field(min_length=1)
    language: str = "en"


class _Section:
    """One heading and the paragraphs under it, while the file is being read."""

    def __init__(self, title: str) -> None:
        self.title = title
        self.paragraphs: list[str] = []

    def add(self, text: str) -> None:
        cleaned = _WHITESPACE.sub(" ", text).strip()
        if cleaned:
            self.paragraphs.append(cleaned)


def suggest_id(filename: str, *, taken: set[str] = frozenset()) -> str:
    """A work id from a filename, not colliding with one already in the library."""
    base = slugify(Path(filename).stem) or "document"
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


# ------------------------------------------------------------------- plain text


def _looks_like_a_heading(line: str) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > _PLAIN_HEADING_MAX_CHARS:
        return False
    # A line ending in sentence punctuation is a sentence, however short.
    return stripped[-1] not in ".,;:!?—"


def _read_plain(text: str) -> list[_Section]:
    """Blank lines separate paragraphs; a short lone line starts a section."""
    sections: list[_Section] = []
    current = _Section("")
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block:
            continue
        if "\n" not in block and _looks_like_a_heading(block):
            if current.paragraphs:
                sections.append(current)
            current = _Section(block)
            continue
        current.add(block)
    if current.paragraphs:
        sections.append(current)
    return sections


# --------------------------------------------------------------------- markdown


def _read_markdown(text: str) -> list[_Section]:
    sections: list[_Section] = []
    current = _Section("")
    buffer: list[str] = []
    fenced = False

    def flush() -> None:
        if buffer:
            current.add(" ".join(buffer))
            buffer.clear()

    for raw in text.splitlines():
        if _FENCE.match(raw.strip()):
            flush()
            fenced = not fenced
            continue
        if fenced:
            continue
        heading = _MD_HEADING.match(raw)
        if heading is not None:
            flush()
            if current.paragraphs:
                sections.append(current)
            current = _Section(_clean_markdown(heading.group(2)))
            continue
        if not raw.strip():
            flush()
            continue
        line = _MD_QUOTE.sub("", raw)
        line = _MD_BULLET.sub("", line)
        buffer.append(_clean_markdown(line))
    flush()
    if current.paragraphs:
        sections.append(current)
    return sections


def _clean_markdown(text: str) -> str:
    """The words, with the notation gone. An image becomes its alt text or nothing."""
    text = _MD_IMAGE.sub(r"\1", text)
    text = _MD_LINK.sub(r"\1", text)
    return _MD_INLINE.sub("", text).strip()


# ------------------------------------------------------------------------- epub


class _XHTMLText(html.parser.HTMLParser):
    """Paragraphs and headings out of one EPUB document, markup left behind.

    `html.parser` rather than a real HTML library because the whole job is "give
    me the text of these tags": the parser is in the standard library, it does not
    care that EPUB's XHTML is stricter than HTML, and a document that is malformed
    enough to defeat it is one the reader should not silently half-import.
    """

    _BLOCKS = frozenset({"p", "div", "li", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6"})
    _HEADINGS = frozenset({"h1", "h2", "h3"})
    _SKIP = frozenset({"script", "style", "head", "title"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        #: `(is_heading, text)` in document order.
        self.blocks: list[tuple[bool, str]] = []
        self._buffer: list[str] = []
        self._heading = False
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._SKIP:
            self._skipping += 1
        elif tag in self._BLOCKS:
            self._flush()
            self._heading = tag in self._HEADINGS
        elif tag == "br":
            self._buffer.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skipping = max(0, self._skipping - 1)
        elif tag in self._BLOCKS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self._buffer.append(data)

    def _flush(self) -> None:
        text = _WHITESPACE.sub(" ", "".join(self._buffer)).strip()
        self._buffer.clear()
        if text:
            self.blocks.append((self._heading, text))
        self._heading = False

    def close(self) -> None:
        super().close()
        self._flush()


def _epub_documents(archive: zipfile.ZipFile) -> list[str]:
    """The reading order: the spine from the OPF, or the zip's own order.

    The spine is the only thing in an EPUB that states what order to read the
    files in, so it is worth the two small parses to find it. A file without a
    usable OPF falls back to sorted names, which is right often enough to be
    better than refusing.
    """
    names = [n for n in archive.namelist() if n.lower().endswith((".xhtml", ".html", ".htm"))]
    opf = next((n for n in archive.namelist() if n.lower().endswith(".opf")), None)
    if opf is None:
        return sorted(names)
    try:
        manifest_xml = archive.read(opf).decode("utf-8", "replace")
    except (KeyError, OSError):  # pragma: no cover - a zip that lied about itself
        return sorted(names)
    # One pass over each `<item …/>`, so `id` and `href` are found whichever order
    # the publisher wrote them in — half the EPUBs in the wild put `href` first.
    ids: dict[str, str] = {}
    for item in re.findall(r"<item\b[^>]*>", manifest_xml):
        item_id = re.search(r'\bid="([^"]+)"', item)
        href = re.search(r'\bhref="([^"]+)"', item)
        if item_id and href:
            ids[item_id.group(1)] = href.group(1)
    order = re.findall(r'<itemref[^>]*idref="([^"]+)"', manifest_xml)
    base = Path(opf).parent
    ordered: list[str] = []
    for ref in order:
        href = ids.get(ref)
        if href is None:
            continue
        candidate = str((base / href).as_posix()).lstrip("/")
        if candidate in archive.namelist():
            ordered.append(candidate)
    return ordered or sorted(names)


def _read_epub(path: Path) -> list[_Section]:
    sections: list[_Section] = []
    current = _Section("")
    with zipfile.ZipFile(path) as archive:
        for name in _epub_documents(archive):
            parser = _XHTMLText()
            parser.feed(archive.read(name).decode("utf-8", "replace"))
            parser.close()
            for is_heading, text in parser.blocks:
                if is_heading:
                    if current.paragraphs:
                        sections.append(current)
                    current = _Section(text)
                else:
                    current.add(text)
    if current.paragraphs:
        sections.append(current)
    return sections


# ------------------------------------------------------------------- the reader


_READERS = {
    ".txt": lambda path: _read_plain(_decoded(path)),
    ".md": lambda path: _read_markdown(_decoded(path)),
    ".markdown": lambda path: _read_markdown(_decoded(path)),
    ".epub": _read_epub,
}


def _decoded(path: Path) -> str:
    """UTF-8 with the BOM eaten, falling back to latin-1 rather than failing.

    A document someone wants to read is not the place to be strict about
    encodings: latin-1 decodes every byte sequence, so the worst case is a few
    odd characters rather than a refusal.
    """
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    return unicodedata.normalize("NFC", text)


def read_document(path: Path) -> list[UnitText]:
    """One file to its chapters. Raises `ValueError` if it holds no readable text.

    The refusal is the important half: an empty list would be written to disk as a
    work with nothing in it, and a library that lists something you cannot open is
    worse than an import that said no.
    """
    path = Path(path)
    reader = _READERS.get(path.suffix.lower())
    if reader is None:
        offered = ", ".join(sorted(DOCUMENT_SUFFIXES))
        raise ValueError(f"cannot read {path.suffix or 'a file with no suffix'}; try {offered}")
    try:
        sections = reader(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError(f"{path.name} could not be opened: {exc}") from exc
    sections = [section for section in sections if section.paragraphs]
    if not sections:
        raise ValueError(f"{path.name} has no readable text in it")
    units: list[UnitText] = []
    for number, section in enumerate(sections, start=1):
        ref = UnitRef(work_id="", book=DOCUMENT_BOOK, chapter=number)
        units.append(
            UnitText(
                ref=ref,
                title=section.title or f"Part {number}",
                verses=[
                    Verse(number=index, text=text)
                    for index, text in enumerate(section.paragraphs, start=1)
                ],
            )
        )
    return units


def import_document(path: Path, spec: DocumentSpec, root: Path) -> WorkRef:
    """Read `path` and file it in the library as a work. Writes nothing on refusal.

    The same layout every work has, so `BibleCorpus` serves it with no idea it was
    ever a Markdown file: `work.yaml`, `source/` holding the original byte for
    byte, `units/DOC/NNN.json`, and an empty `derived/`.
    """
    from videomaker.cache import _write_atomic

    units = read_document(path)
    work = WorkRef(
        id=spec.work_id,
        title=spec.title,
        language=spec.language,
        licence=OWN_FILE_LICENCE,
        licence_url=OWN_FILE_URL,
        source_url=f"file://{Path(path).name}",
    )
    target = work_dir(root, work.id)
    (target / SOURCE_DIRNAME).mkdir(parents=True, exist_ok=True)
    (target / DERIVED_DIRNAME).mkdir(exist_ok=True)
    for stale in (target / UNITS_DIRNAME).glob("*/*.json"):
        stale.unlink()
    (target / SOURCE_DIRNAME / Path(path).name).write_bytes(Path(path).read_bytes())
    for unit in units:
        stamped = UnitText(
            ref=unit.ref.model_copy(update={"work_id": work.id}),
            title=unit.title,
            verses=unit.verses,
        )
        _write_atomic(unit_path(target, stamped.ref), stamped.model_dump_json(indent=1))
    import yaml

    _write_atomic(target / WORK_FILE, yaml.safe_dump(work.model_dump(mode="json"), sort_keys=False))
    return work
