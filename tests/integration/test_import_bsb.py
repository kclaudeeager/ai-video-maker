"""Importing the Berean Standard Bible from berean.bible, for real.

Downloads ~4 MB and parses 66 books, so it is `slow`, and it runs only under
`VIDEOMAKER_NETWORK_TESTS=1`: the suite is offline by contract and CI must not
depend on berean.bible answering.

**This is the regression test for the two constructs that made the BSB
unimportable.** `tests/fixtures/usfm/41-MRK.usfm` reproduces their shape offline
and is what runs on every commit; this one is the periodic check that the shape
in the fixture is still the shape in the archive. A publisher can change their
export, and a fixture cannot notice.

Measured against the published archive on 2026-09-10, before `normalise_markup`:

* 53 of the 66 books raised `IndexError` out of
  `usfm_grammar.usj_generator.node_2_usj_generic` on `\\ref display|TARGET\\ref*`;
* the remaining six — Matthew, Mark, Luke, John, Acts and Revelation — raised on
  a `\\wj ... \\wj*` span that opens before a verse marker and closes after it.

So a green run here means 66 books parsed, not 13.
"""

import json
import os
import re

import pytest

from videomaker.corpus.catalogue import CATALOGUE
from videomaker.corpus.importer import import_work, work_dir
from videomaker.corpus.usfm import normalise_markup

PROTESTANT_CANON_CHAPTERS = 1189
NETWORK_OPT_IN = "VIDEOMAKER_NETWORK_TESTS"

#: The six books whose `\wj` spans straddle a verse boundary — every one of them
#: parsed to nothing before the fix, so their presence is the assertion.
WORDS_OF_JESUS_BOOKS = ("MAT", "MRK", "LUK", "JHN", "ACT", "REV")

#: Mark 1:15 is the worked example in `corpus/usfm.py`'s docstring: the verse
#: marker sits *inside* the words-of-Jesus span that opens at the end of verse 14.
STRADDLED = ("MRK", "001", 15, "The time is fulfilled")


@pytest.fixture(scope="module")
def imported(tmp_path_factory):
    """One download for the whole module: it is ~4 MB and 66 books of parsing.

    The grammar error is caught and re-raised as a *failure* rather than left to
    surface as a fixture error. The distinction is worth the four lines: an error
    here reads as broken test plumbing, and the thing it actually means is that
    the importer can no longer read a published Bible — which is the exact
    regression this file exists to name.
    """
    if not os.environ.get(NETWORK_OPT_IN):
        pytest.skip(f"set {NETWORK_OPT_IN}=1 to download")
    root = tmp_path_factory.mktemp("bsb")
    try:
        work = import_work(CATALOGUE["bsb"], root)
    except Exception as exc:  # noqa: BLE001 - the point is to relabel anything
        pytest.fail(
            f"the BSB no longer imports: {type(exc).__name__}: {exc}\n\n"
            "This is what `corpus/usfm.normalise_markup` exists to prevent. Either "
            "it has stopped rewriting `\\ref …|…\\ref*` and `\\wj …\\wj*`, or the "
            "publisher's export has grown a third construct the grammar cannot "
            "read — check which books fail and add the shape to "
            "tests/fixtures/usfm/41-MRK.usfm so it is caught offline."
        )
    return work, work_dir(root, work.id)


def _unit(root, book: str, chapter: str) -> dict:
    return json.loads((root / "units" / book / f"{chapter}.json").read_text())


@pytest.mark.slow
def test_every_book_of_the_canon_arrives(imported):
    """66 books, not the 13 that parsed before `normalise_markup`."""
    _work, root = imported
    books = sorted(path.name for path in (root / "units").iterdir() if path.is_dir())

    assert len(books) == 66
    assert sum(1 for _ in (root / "units").glob("*/*.json")) == PROTESTANT_CANON_CHAPTERS
    for book in WORDS_OF_JESUS_BOOKS:
        assert book in books, f"{book} is one of the six that parsed to nothing"


@pytest.mark.slow
def test_a_verse_inside_a_straddling_words_of_jesus_span_is_whole(imported):
    """The exact verse `corpus/usfm.py` names: `\\wj` opens at the end of verse 14
    and closes inside verse 15, which is what defeated the grammar."""
    _work, root = imported
    book, chapter, number, opening = STRADDLED
    unit = _unit(root, book, chapter)

    verse = next(v for v in unit["verses"] if v["number"] == number)
    assert opening in verse["text"]
    assert verse["text"].rstrip().endswith("!”"), "and it runs to the end of the quotation"
    assert "\\" not in verse["text"]


@pytest.mark.slow
def test_no_markup_reaches_any_verse_of_a_gospel(imported):
    """A backslash in the text is a marker the parse left behind."""
    _work, root = imported
    for book in WORDS_OF_JESUS_BOOKS:
        for path in sorted((root / "units" / book).glob("*.json")):
            for verse in json.loads(path.read_text())["verses"]:
                assert "\\" not in verse["text"], f"{book} {path.stem}:{verse['number']}"
                assert "|" not in verse["text"], "a `\\ref` target survived the rewrite"


@pytest.mark.slow
def test_a_cross_reference_line_reaches_no_verse(imported):
    """`\\ref` keeps its display text, but the `\\r` line it sits in is apparatus:
    the BCV+TEXT filter drops the line, so neither half should be in the text."""
    _work, root = imported
    john = _unit(root, "JHN", "003")
    spoken = " ".join(verse["text"] for verse in john["verses"])

    assert "Nicodemus" in spoken, "the chapter itself is there"
    assert "LUK " not in spoken and "MAT " not in spoken, "no USFM book codes in the prose"


@pytest.mark.slow
def test_the_real_archive_still_carries_both_constructs(imported):
    """The fixture claims these shapes exist in the wild. This checks they do.

    Without it the offline fixture could drift into testing a shape the publisher
    has since dropped — a green suite guarding nothing. `source/` is the archive
    exactly as it arrived, so it is the honest place to look.
    """
    _work, root = imported
    mark = (root / "source" / "MRK.usfm").read_text(encoding="utf-8-sig")

    assert re.search(r"\\wj\b", mark), "the words-of-Jesus marker"
    assert re.search(r"\\wj[^\\]*\\v ", mark), "and a span that straddles a verse marker"
    john = (root / "source" / "JHN.usfm").read_text(encoding="utf-8-sig")
    assert re.search(r"\\ref\s+[^|\\]*\|[^\\]*\\ref\*", john), "the inline scripture link"


@pytest.mark.slow
def test_the_rewrite_drops_a_real_targets_half(imported):
    """`\ref` keeps its display text and loses its target — checked on the
    archive's own bytes rather than on a fixture, because the offline test cannot
    see whether the publisher writes them the way we assume."""
    _work, root = imported
    john = (root / "source" / "JHN.usfm").read_text(encoding="utf-8-sig")
    ref = re.search(r"\\ref\s+([^|\\]*)\|([^\\]*)\\ref\*", john)
    assert ref is not None
    display, target = ref.group(1), ref.group(2)

    rewritten = normalise_markup(ref.group(0))

    assert rewritten == display
    assert target not in rewritten, "the USFM book code must not survive as prose"
    assert "|" not in rewritten


@pytest.mark.slow
def test_the_licence_the_catalogue_states_is_what_is_recorded(imported):
    """The gate applies to a live import exactly as it does to a fixture."""
    work, root = imported

    assert work.licence == CATALOGUE["bsb"].licence
    assert work.licence.strip().endswith(".")
    assert (root / "work.yaml").is_file()
    assert (root / "source").is_dir(), "the archive is kept as it arrived"
