"""Your own document, read as a work.

This is the module that proves the reader generalises past scripture: once a file
is chapters-of-numbered-units, `BibleCorpus`, the brief, the narration and the
verse highlighting all work on it with no idea it was ever Markdown. The last test
in this file is that claim, executed.
"""

import zipfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from videomaker.config import Settings
from videomaker.corpus.bible import BibleCorpus
from videomaker.corpus.documents import (
    DOCUMENT_BOOK,
    DOCUMENT_SUFFIXES,
    OWN_FILE_LICENCE,
    DocumentSpec,
    import_document,
    read_document,
    suggest_id,
)
from videomaker.corpus.importer import read_work, work_dir
from videomaker.corpus.models import UnitRef

MARKDOWN = """\
# The first part

A paragraph of prose that runs
across two source lines.

Another paragraph, with a [link](https://example.invalid) and some *emphasis*.

```bash
echo "a narrator must not read this aloud"
```

## The second part

- a bullet
- another bullet

> a quoted line
"""

PLAIN = """\
Opening Titles

The first paragraph of the plain text file.

A second paragraph under the same heading.

Chapter Two

Something else entirely.
"""


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def write_epub(tmp_path: Path, name: str = "book.epub") -> Path:
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        # The filenames sort *against* the spine — `a-closing` before `z-opening`
        # alphabetically, the other way round in reading order — so a parser that
        # ignored the spine would visibly read the book backwards. The two
        # `<item>` elements also put `id` and `href` in opposite orders, because
        # half the EPUBs in the wild do.
        archive.writestr(
            "OEBPS/content.opf",
            """<package><manifest>
                 <item href="a-closing.xhtml" id="closing" media-type="application/xhtml+xml"/>
                 <item id="opening" href="z-opening.xhtml" media-type="application/xhtml+xml"/>
               </manifest>
               <spine><itemref idref="opening"/><itemref idref="closing"/></spine></package>""",
        )
        archive.writestr(
            "OEBPS/z-opening.xhtml",
            "<html><head><title>ignored</title><style>p{color:red}</style></head>"
            "<body><h1>The Opening</h1><p>First <em>paragraph</em> of the book.</p>"
            "<p>Second paragraph.</p></body></html>",
        )
        archive.writestr(
            "OEBPS/a-closing.xhtml",
            "<html><body><h2>The Closing</h2><p>Last paragraph.</p></body></html>",
        )
    return path


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(workspace_dir=tmp_path / "workspace", provider_chains={"corpus": ["bible"]})


# ------------------------------------------------------------------- the shape


def test_markdown_headings_become_sections_and_paragraphs_become_units(tmp_path):
    units = read_document(write(tmp_path, "notes.md", MARKDOWN))

    assert [unit.title for unit in units] == ["The first part", "The second part"]
    assert [unit.ref.chapter for unit in units] == [1, 2]
    first = units[0]
    assert [verse.number for verse in first.verses] == [1, 2]
    # A paragraph is one unit even when the source wrapped it across lines.
    assert first.verses[0].text == "A paragraph of prose that runs across two source lines."


def test_markdown_notation_never_reaches_the_words(tmp_path):
    units = read_document(write(tmp_path, "notes.md", MARKDOWN))
    spoken = " ".join(verse.text for unit in units for verse in unit.verses)

    assert "example.invalid" not in spoken, "a narrator must not read a URL aloud"
    assert "link" in spoken, "...but the words of the link survive"
    assert "*" not in spoken and "`" not in spoken and ">" not in spoken
    assert "echo" not in spoken, "a fenced code block is not the document"


def test_plain_text_takes_a_short_lone_line_as_a_heading(tmp_path):
    units = read_document(write(tmp_path, "book.txt", PLAIN))

    assert [unit.title for unit in units] == ["Opening Titles", "Chapter Two"]
    assert len(units[0].verses) == 2
    assert units[1].verses[0].text == "Something else entirely."


def test_a_short_line_that_is_a_sentence_is_not_a_heading(tmp_path):
    units = read_document(write(tmp_path, "p.txt", "It rained.\n\nThen it stopped.\n"))

    assert len(units) == 1
    assert [verse.text for verse in units[0].verses] == ["It rained.", "Then it stopped."]


def test_an_epub_is_read_in_spine_order_with_no_markup(tmp_path):
    units = read_document(write_epub(tmp_path))

    # Spine order, not filename order: the fixture's names sort the other way, so
    # a parser that read the zip alphabetically would fail this line.
    assert [unit.title for unit in units] == ["The Opening", "The Closing"]
    spoken = " ".join(verse.text for unit in units for verse in unit.verses)
    assert "<" not in spoken and "color:red" not in spoken
    assert "ignored" not in spoken, "a <title> is not part of the text"
    assert "First paragraph of the book." in spoken


def test_a_document_with_no_heading_still_reads(tmp_path):
    units = read_document(write(tmp_path, "flat.md", "Just one paragraph, no heading.\n"))

    assert len(units) == 1
    assert units[0].title == "Part 1"


# --------------------------------------------------------------------- refusals


def test_a_file_with_nothing_readable_is_refused(tmp_path):
    with pytest.raises(ValueError, match="no readable text"):
        read_document(write(tmp_path, "empty.md", "\n\n   \n"))


def test_an_unreadable_suffix_names_what_can_be_read(tmp_path):
    with pytest.raises(ValueError, match=r"\.md"):
        read_document(write(tmp_path, "slides.pptx", "whatever"))


def test_a_file_pretending_to_be_an_epub_is_refused(tmp_path):
    with pytest.raises(ValueError, match="could not be opened"):
        read_document(write(tmp_path, "fake.epub", "this is not a zip"))


def test_a_refused_document_writes_nothing(tmp_path, settings):
    spec = DocumentSpec(work_id="empty", title="Empty")
    with pytest.raises(ValueError):
        import_document(write(tmp_path, "empty.md", "   \n"), spec, settings.workspace_dir)

    assert not work_dir(settings.workspace_dir, "empty").exists()


def test_a_spec_needs_a_usable_id():
    with pytest.raises(ValidationError):
        DocumentSpec(work_id="Not An Id", title="X")
    with pytest.raises(ValidationError):
        DocumentSpec(work_id="ok", title="")


# ------------------------------------------------------------------ the licence


def test_the_licence_is_stated_rather_than_blank(tmp_path, settings):
    work = import_document(
        write(tmp_path, "notes.md", MARKDOWN),
        DocumentSpec(work_id="notes", title="Notes"),
        settings.workspace_dir,
    )

    assert work.licence == OWN_FILE_LICENCE
    assert work.licence.strip().endswith(".")
    # The gate is `WorkRef`'s own validator, and it is what refuses a blank one.
    assert read_work(work_dir(settings.workspace_dir, "notes")) == work


# ------------------------------------------------------------------ on the disk


def test_the_original_file_is_kept_byte_for_byte(tmp_path, settings):
    source = write(tmp_path, "notes.md", MARKDOWN)
    import_document(source, DocumentSpec(work_id="notes", title="Notes"), settings.workspace_dir)

    kept = work_dir(settings.workspace_dir, "notes") / "source" / "notes.md"
    assert kept.read_bytes() == source.read_bytes()


def test_re_importing_replaces_the_sections(tmp_path, settings):
    spec = DocumentSpec(work_id="notes", title="Notes")
    import_document(write(tmp_path, "notes.md", MARKDOWN), spec, settings.workspace_dir)
    import_document(
        write(tmp_path, "notes.md", "# Only one now\n\nA paragraph.\n"),
        spec,
        settings.workspace_dir,
    )

    units = sorted((work_dir(settings.workspace_dir, "notes") / "units" / DOCUMENT_BOOK).glob("*.json"))
    assert len(units) == 1


def test_suggest_id_avoids_a_collision():
    assert suggest_id("Field Notes.md") == "field-notes"
    assert suggest_id("Field Notes.md", taken={"field-notes"}) == "field-notes-2"
    assert suggest_id("!!!.md") == "document"


def test_every_readable_suffix_is_offered():
    assert DOCUMENT_SUFFIXES == frozenset({".txt", ".md", ".markdown", ".epub"})


# ------------------------------- and the reader does not know it was a document


def test_the_reader_serves_a_document_like_any_other_work(tmp_path, settings):
    import_document(
        write(tmp_path, "notes.md", MARKDOWN),
        DocumentSpec(work_id="notes", title="Notes"),
        settings.workspace_dir,
    )
    corpus = BibleCorpus(settings)

    (work,) = corpus.works()
    assert work.id == "notes"
    outline = corpus.outline("notes")
    assert [ref.key() for ref in outline] == ["notes/DOC/001", "notes/DOC/002"]

    unit = corpus.unit(UnitRef(work_id="notes", book=DOCUMENT_BOOK, chapter=1))
    assert unit.title == "The first part"
    assert unit.word_count > 0
    # `plain` is what a narrator is given and what a brief is written from.
    assert "paragraph of prose" in unit.plain
