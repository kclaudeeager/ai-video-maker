"""Reference parsing: the round trip, and the two books that must not be confused.

`test_jn_is_john_and_jon_is_jonah` is the whole reason this module refuses fuzzy
matching. The two spellings are one edit apart, and a Levenshtein fallback would
resolve both to whichever it saw first — handing the reader a real chapter of the
wrong book, with nothing about the page looking broken.
"""

import pytest

from videomaker.corpus.refs import (
    BOOK_NAMES,
    BOOK_ORDER,
    NEAREST_SUGGESTIONS,
    book_name,
    format_reference,
    parse_reference,
)

WORK = "web"


def parse(text: str):
    return parse_reference(text, work_id=WORK)


# ------------------------------------------------------------------- the tables


def test_the_canon_is_sixty_six_books_in_order():
    assert len(BOOK_ORDER) == 66
    assert len(set(BOOK_ORDER)) == 66
    assert BOOK_ORDER[0] == "GEN"
    assert BOOK_ORDER[-1] == "REV"


def test_every_code_is_its_own_accepted_spelling():
    # The USFM code is what lands in a directory name and a URL, so it has to be
    # something a reader can type back in.
    assert all(BOOK_NAMES[code.casefold()] == code for code in BOOK_ORDER)


@pytest.mark.parametrize("spelling", sorted(BOOK_NAMES))
def test_every_accepted_spelling_round_trips(spelling):
    first = parse(f"{spelling} 3")
    second = parse(format_reference(first))
    assert first == second
    assert first.book == BOOK_NAMES[spelling]


# --------------------------------------------------------------- accepted forms


@pytest.mark.parametrize(
    ("text", "book", "chapter", "verses"),
    [
        ("John 3", "JHN", 3, None),
        ("Jn 3:16", "JHN", 3, (16, 16)),
        ("John 3:16-18", "JHN", 3, (16, 18)),
        ("JHN 3", "JHN", 3, None),
        ("1 Cor 13", "1CO", 13, None),
        ("I Corinthians 13", "1CO", 13, None),
        ("1co13", "1CO", 13, None),
        ("  john   3  ", "JHN", 3, None),
    ],
)
def test_the_documented_forms_parse(text, book, chapter, verses):
    ref = parse(text)
    assert (ref.work_id, ref.book, ref.chapter, ref.verses) == (WORK, book, chapter, verses)


def test_jn_is_john_and_jon_is_jonah():
    assert parse("Jn 1").book == "JHN"
    assert parse("Jon 1").book == "JON"


def test_a_leading_roman_numeral_is_only_mapped_as_a_whole_token():
    # "Isaiah" starts with an I; "1saiah" is not a book.
    assert parse("Isaiah 40").book == "ISA"
    assert parse("II Timothy 2").book == "2TI"


def test_a_chapter_out_of_range_still_parses():
    # Whether John has 99 chapters is the corpus's question, not the parser's.
    assert parse("Jhn 99").chapter == 99


def test_formatting_names_the_book_the_way_a_reader_writes_it():
    assert format_reference(parse("1co13")) == "1 Corinthians 13"
    assert format_reference(parse("Jn 3:16")) == "John 3:16"
    assert format_reference(parse("Jn 3:16-18")) == "John 3:16-18"
    assert book_name("JHN") == "John"


# -------------------------------------------------------------------- refusals


def test_an_unknown_book_names_the_input_and_suggests_nothing_it_did_not_read():
    with pytest.raises(ValueError) as exc:
        parse("Gospel of Fred 3")
    message = str(exc.value)
    assert "Gospel of Fred 3" in message
    # Alphabetical neighbours, not an edit-distance guess at what was meant.
    suggested = message.rsplit(":", 1)[1].split(",")
    assert len(suggested) == NEAREST_SUGGESTIONS
    assert all(name.strip() in BOOK_NAMES for name in suggested)


def test_something_that_is_not_a_reference_at_all_names_the_input():
    with pytest.raises(ValueError, match="Gospel of Fred"):
        parse("Gospel of Fred")


def test_a_chapter_of_zero_is_refused():
    with pytest.raises(ValueError):
        parse("John 0")


def test_an_unknown_code_cannot_be_named():
    with pytest.raises(ValueError, match="ZZZ"):
        book_name("ZZZ")
