"""Turning what a reader types into a `UnitRef`, and back into what a reader reads.

**Exact table lookup only. No fuzzy matching, ever.** `"Jn"` is John and `"Jon"` is
Jonah; they are one edit apart, and an edit-distance match will confuse them
silently — which is worse than an error, because the reader gets a real chapter of
the wrong book and nothing looks broken. So an unrecognised name raises, and the
message lists the three alphabetically nearest *accepted spellings* rather than
guessing between them.

Normalisation is exactly three things: case-folding, whitespace stripping, and
mapping a leading `I`/`II`/`III` token to `1`/`2`/`3`. The roman numeral is mapped
per *token* rather than per prefix, because `"Isaiah"` starts with an `I` too and
`"1saiah"` is not a book.
"""

import re
from bisect import bisect_left

from videomaker.corpus.models import UnitRef

#: How many accepted spellings an unknown name is answered with. Three is enough to
#: show the shape of what the table wants without printing a wall of 200 aliases.
NEAREST_SUGGESTIONS = 3

#: Canonical order, display name, and the spellings that reach it.
#:
#: The USFM code is always accepted (case-folded), which is why `jud` is Jude rather
#: than Judges: `JUD` is Jude's code and `JDG` is Judges'. Judges is reached by
#: `judg` or `judges`. Making the code mean anything else would put the parser and
#: the on-disk directory names into disagreement, which is the one collision nobody
#: would think to look for.
_BOOKS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("GEN", "Genesis", ("gn", "ge")),
    ("EXO", "Exodus", ("ex", "exod")),
    ("LEV", "Leviticus", ("lv",)),
    ("NUM", "Numbers", ("nm", "nb")),
    ("DEU", "Deuteronomy", ("dt", "deut")),
    ("JOS", "Joshua", ("josh",)),
    ("JDG", "Judges", ("judg",)),
    ("RUT", "Ruth", ("ru",)),
    ("1SA", "1 Samuel", ("1sam", "1sm")),
    ("2SA", "2 Samuel", ("2sam", "2sm")),
    ("1KI", "1 Kings", ("1kgs", "1kin", "1kings")),
    ("2KI", "2 Kings", ("2kgs", "2kin", "2kings")),
    ("1CH", "1 Chronicles", ("1chr", "1chron")),
    ("2CH", "2 Chronicles", ("2chr", "2chron")),
    ("EZR", "Ezra", ()),
    ("NEH", "Nehemiah", ("ne",)),
    ("EST", "Esther", ("esth",)),
    ("JOB", "Job", ("jb",)),
    ("PSA", "Psalms", ("ps", "psalm", "pss")),
    ("PRO", "Proverbs", ("pr", "prv", "prov")),
    ("ECC", "Ecclesiastes", ("ec", "eccl", "qoh")),
    ("SNG", "Song of Songs", ("song", "songs", "sos", "songofsolomon", "canticles")),
    ("ISA", "Isaiah", ("is",)),
    ("JER", "Jeremiah", ("je", "jr")),
    ("LAM", "Lamentations", ("la",)),
    ("EZK", "Ezekiel", ("eze", "ezek")),
    ("DAN", "Daniel", ("da", "dn")),
    ("HOS", "Hosea", ("ho",)),
    ("JOL", "Joel", ("jl",)),
    ("AMO", "Amos", ("am",)),
    ("OBA", "Obadiah", ("ob", "obad")),
    ("JON", "Jonah", ("jnh",)),
    ("MIC", "Micah", ("mi",)),
    ("NAM", "Nahum", ("na", "nah")),
    ("HAB", "Habakkuk", ("hb",)),
    ("ZEP", "Zephaniah", ("zeph", "zp")),
    ("HAG", "Haggai", ("hg",)),
    ("ZEC", "Zechariah", ("zech", "zc")),
    ("MAL", "Malachi", ("ml",)),
    ("MAT", "Matthew", ("mt", "matt")),
    ("MRK", "Mark", ("mk", "mar")),
    ("LUK", "Luke", ("lk", "lu")),
    ("JHN", "John", ("jn", "jo")),
    ("ACT", "Acts", ("ac", "acts")),
    ("ROM", "Romans", ("rm", "ro")),
    ("1CO", "1 Corinthians", ("1cor", "1co")),
    ("2CO", "2 Corinthians", ("2cor", "2co")),
    ("GAL", "Galatians", ("ga",)),
    ("EPH", "Ephesians", ("ep",)),
    # `phil` is Philippians and `phlm` is Philemon: the long-standing convention,
    # and the reason Philemon is never abbreviated past four letters here.
    ("PHP", "Philippians", ("phil", "php", "pp")),
    ("COL", "Colossians", ("cl",)),
    ("1TH", "1 Thessalonians", ("1thess", "1thes", "1th")),
    ("2TH", "2 Thessalonians", ("2thess", "2thes", "2th")),
    ("1TI", "1 Timothy", ("1tim", "1ti")),
    ("2TI", "2 Timothy", ("2tim", "2ti")),
    ("TIT", "Titus", ("ti",)),
    ("PHM", "Philemon", ("phlm", "pm")),
    ("HEB", "Hebrews", ()),
    ("JAS", "James", ("jam", "jm")),
    ("1PE", "1 Peter", ("1pet", "1pt")),
    ("2PE", "2 Peter", ("2pet", "2pt")),
    ("1JN", "1 John", ("1jhn", "1jo", "1joh")),
    ("2JN", "2 John", ("2jhn", "2jo", "2joh")),
    ("3JN", "3 John", ("3jhn", "3jo", "3joh")),
    ("JUD", "Jude", ("jude",)),
    ("REV", "Revelation", ("rv", "re", "apocalypse")),
)

#: The 66 USFM codes in canonical order — what `outline` sorts by and what the web
#: layer validates a URL segment against.
BOOK_ORDER: tuple[str, ...] = tuple(code for code, _title, _aliases in _BOOKS)

_TITLES: dict[str, str] = {code: title for code, title, _aliases in _BOOKS}

#: Leading roman numerals, mapped as whole tokens only.
_ROMAN = {"i": "1", "ii": "2", "iii": "3"}

#: `"john3:16-18"` — the book is non-greedy so the numbered books keep their digit.
_REFERENCE = re.compile(
    r"^(?P<book>.*?)(?P<chapter>\d+)(?::(?P<first>\d+)(?:-(?P<last>\d+))?)?$"
)


def _normalise(text: str) -> str:
    tokens = text.casefold().split()
    if tokens and tokens[0] in _ROMAN:
        tokens[0] = _ROMAN[tokens[0]]
    return "".join(tokens)


def _build_names() -> dict[str, str]:
    """Every accepted spelling to its code, refusing to let two books share one.

    Built rather than written out so the code, the display name and the aliases of a
    book stay on one line of `_BOOKS`. The collision check is the point: a duplicate
    key would otherwise be a silent last-one-wins, which is exactly the class of
    quiet wrongness this module exists to refuse.
    """
    names: dict[str, str] = {}
    for code, title, aliases in _BOOKS:
        for spelling in (code, title, *aliases):
            key = _normalise(spelling)
            if key in names and names[key] != code:
                raise ValueError(f"{key!r} claims both {names[key]} and {code}")
            names[key] = code
    return names


#: Normalised name or abbreviation -> USFM code.
BOOK_NAMES: dict[str, str] = _build_names()

_SORTED_NAMES: tuple[str, ...] = tuple(sorted(BOOK_NAMES))


def _nearest(name: str) -> list[str]:
    """The accepted spellings either side of `name` alphabetically.

    Alphabetical neighbours rather than an edit-distance ranking, deliberately: an
    edit-distance suggestion is one keystroke away from being an edit-distance
    *match*, and this module does not do those. Neighbours help a typo and never
    pretend to know which book was meant.
    """
    index = bisect_left(_SORTED_NAMES, name)
    start = min(max(index - 1, 0), max(len(_SORTED_NAMES) - NEAREST_SUGGESTIONS, 0))
    return list(_SORTED_NAMES[start : start + NEAREST_SUGGESTIONS])


def book_name(code: str) -> str:
    """`"JHN"` -> `"John"`. Raises `ValueError` on a code no canon here knows."""
    try:
        return _TITLES[code]
    except KeyError:
        raise ValueError(f"unknown book code {code!r}") from None


def book_label(code: str) -> str:
    """`book_name`, but never raising — the code itself when no canon knows it.

    A document imported from your own disk is filed under a code that is
    deliberately not a book of any Bible (`corpus/documents.DOCUMENT_BOOK`), so
    every *display* path needs an answer for it while `book_name` keeps its
    contract of refusing a code it does not recognise.
    """
    return _TITLES.get(code, code)


def parse_reference(text: str, *, work_id: str) -> UnitRef:
    """`"1 Cor 13"` -> `UnitRef(work_id=..., book="1CO", chapter=13)`.

    Range checking is not done here and must not be: whether `Jhn 99` exists is a
    property of the work on disk, so the parser accepts it and the corpus is what
    raises. A parser that knew chapter counts would have to be re-checked against
    every versification in `Versification`.
    """
    normalised = _normalise(text)
    match = _REFERENCE.match(normalised)
    if match is None:
        raise ValueError(f"cannot read a reference from {text!r}: try 'John 3' or 'John 3:16-18'")
    code = BOOK_NAMES.get(match["book"])
    if code is None:
        nearest = ", ".join(_nearest(match["book"]))
        raise ValueError(f"unknown book in {text!r}; nearest names known: {nearest}")
    first, last = match["first"], match["last"]
    verses = None if first is None else (int(first), int(last or first))
    return UnitRef(work_id=work_id, book=code, chapter=int(match["chapter"]), verses=verses)


def format_reference(ref: UnitRef) -> str:
    """`"John 3:16-18"` — the form a reader would write, and `parse_reference` reads."""
    title = book_label(ref.book)
    if ref.verses is None:
        return f"{title} {ref.chapter}"
    first, last = ref.verses
    if first == last:
        return f"{title} {ref.chapter}:{first}"
    return f"{title} {ref.chapter}:{first}-{last}"
