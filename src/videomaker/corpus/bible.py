"""The library on disk, read as a `CorpusProvider`.

Holds no scripture in Python source and never will: every word it serves was
imported by `corpus/importer.py` into the user's own `workspace/library/`, behind
the licence gate. This module only knows the layout.

`outline` is in `BOOK_ORDER` order rather than directory order because a filesystem
sorts `1CO` before `GEN`, and a reader opening a Bible expects Genesis first. Books
the canon table does not know — a deuterocanonical file in an archive that carries
one — are listed after the 66, by code, rather than dropped: the text was imported
under a licence and hiding it would be the odd choice.
"""

from pathlib import Path

from pydantic import ValidationError

from videomaker.config import Settings
from videomaker.corpus.importer import UNITS_DIRNAME, list_works, unit_path, work_dir
from videomaker.corpus.models import UnitRef, UnitText, WorkRef
from videomaker.corpus.refs import BOOK_ORDER, format_reference
from videomaker.providers import register
from videomaker.providers.base import CorpusProvider

PROVIDER_NAME = "bible"

_CANON_RANK = {code: index for index, code in enumerate(BOOK_ORDER)}


def _book_rank(code: str) -> tuple[int, str]:
    return (_CANON_RANK.get(code, len(BOOK_ORDER)), code)


@register("corpus", PROVIDER_NAME)
class BibleCorpus(CorpusProvider):
    """Reads `<workspace>/library/`. Empty library, empty answers — never an error."""

    def __init__(self, settings: Settings) -> None:
        self.root = Path(settings.workspace_dir)

    def works(self) -> list[WorkRef]:
        return [work for work, _chapters in list_works(self.root)]

    def outline(self, work_id: str) -> list[UnitRef]:
        units = work_dir(self.root, work_id) / UNITS_DIRNAME
        if not units.is_dir():
            return []
        refs: list[UnitRef] = []
        for book in sorted(
            (p for p in units.iterdir() if p.is_dir()), key=lambda p: _book_rank(p.name)
        ):
            for file in sorted(book.glob("*.json")):
                if file.stem.isdigit():
                    refs.append(UnitRef(work_id=work_id, book=book.name, chapter=int(file.stem)))
        return refs

    def unit(self, ref: UnitRef) -> UnitText:
        path = unit_path(work_dir(self.root, ref.work_id), ref)
        try:
            unit = UnitText.model_validate_json(path.read_text())
        except (OSError, ValidationError):
            raise KeyError(f"{format_reference(ref)} is not in the library ({ref.key()})") from None
        if ref.verses is None:
            return unit
        first, last = ref.verses
        verses = [verse for verse in unit.verses if first <= verse.number <= last]
        return UnitText(ref=ref, title=format_reference(ref), verses=verses)
