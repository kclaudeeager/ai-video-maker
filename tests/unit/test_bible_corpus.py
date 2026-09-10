"""`BibleCorpus` over a real import of the fixture, and the catalogue's shape.

The catalogue test is the licence rule applied to ourselves: every row is an
`ImportSpec`, so a row without a licence cannot be written — and the test that
every row's licence is one sentence is what keeps "see website" from creeping in.
"""

from pathlib import Path

import pytest

from videomaker.config import Settings
from videomaker.corpus import importer as importer_module
from videomaker.corpus.bible import BibleCorpus
from videomaker.corpus.catalogue import CATALOGUE
from videomaker.corpus.importer import ImportSpec, import_work
from videomaker.corpus.models import UnitRef
from videomaker.corpus.refs import BOOK_ORDER
from videomaker.providers import get_provider
from videomaker.providers.base import CorpusProvider

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "usfm"


@pytest.fixture(autouse=True)
def _own_notice(tmp_path, monkeypatch):
    monkeypatch.setattr(importer_module, "NOTICE_PATH", tmp_path / "NOTICE.md")


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(workspace_dir=tmp_path / "workspace", provider_chains={"corpus": ["bible"]})


@pytest.fixture
def corpus(settings) -> BibleCorpus:
    provider = get_provider("corpus", "bible", settings)
    assert isinstance(provider, CorpusProvider)
    return provider


@pytest.fixture
def imported(settings):
    spec = CATALOGUE["fixture"].model_copy(update={"archive": str(FIXTURE_DIR)})
    return import_work(spec, settings.workspace_dir)


# ------------------------------------------------------------------- the library


def test_an_empty_library_is_empty_rather_than_an_error(corpus):
    assert corpus.works() == []
    assert corpus.outline("fixture") == []


def test_works_lists_what_was_imported(corpus, imported):
    assert corpus.works() == [imported]


def test_the_outline_is_in_canonical_book_order(corpus, imported, settings):
    # GEN and JHN sort the same way alphabetically and canonically, so on the
    # fixture alone a directory sort would pass. Acts does not: `ACT` sorts first
    # by name and 44th in the canon, which is what makes this assertion bite.
    acts = settings.workspace_dir / "library" / "fixture" / "units" / "ACT" / "001.json"
    acts.parent.mkdir()
    acts.write_text("{}")
    outline = corpus.outline("fixture")
    assert [ref.key() for ref in outline] == [
        "fixture/GEN/001",
        "fixture/GEN/002",
        "fixture/JHN/001",
        "fixture/JHN/002",
        "fixture/JHN/003",
        "fixture/ACT/001",
    ]
    ranks = [BOOK_ORDER.index(ref.book) for ref in outline]
    assert ranks == sorted(ranks)


def test_a_book_outside_the_canon_is_listed_last_not_dropped(corpus, imported, settings):
    extra = settings.workspace_dir / "library" / "fixture" / "units" / "TOB" / "001.json"
    extra.parent.mkdir()
    extra.write_text("{}")
    assert [ref.book for ref in corpus.outline("fixture")][-1] == "TOB"


def test_a_unit_reads_back_its_verses(corpus, imported):
    unit = corpus.unit(UnitRef(work_id="fixture", book="JHN", chapter=3))
    assert unit.title == "John 3"
    assert [verse.number for verse in unit.verses] == [1, 2, 3]
    assert unit.ref.work_id == "fixture"


def test_a_verse_range_narrows_the_unit(corpus, imported):
    unit = corpus.unit(UnitRef(work_id="fixture", book="JHN", chapter=3, verses=(2, 3)))
    assert unit.title == "John 3:2-3"
    assert [verse.number for verse in unit.verses] == [2, 3]


def test_a_missing_chapter_raises_naming_the_reference(corpus, imported):
    with pytest.raises(KeyError, match="John 99"):
        corpus.unit(UnitRef(work_id="fixture", book="JHN", chapter=99))
    with pytest.raises(KeyError, match="Genesis 1"):
        corpus.unit(UnitRef(work_id="nothing", book="GEN", chapter=1))


def test_the_bible_corpus_is_the_default_chain(settings):
    assert Settings().provider_chains["corpus"] == ["bible"]


# ----------------------------------------------------------------- the catalogue


def test_the_catalogue_carries_the_two_blessed_texts_and_the_fixture():
    assert set(CATALOGUE) >= {"web", "bsb", "fixture"}
    assert CATALOGUE["web"].language == "en"
    assert CATALOGUE["bsb"].language == "en"


@pytest.mark.parametrize("work_id", sorted(CATALOGUE))
def test_every_row_states_its_licence_in_one_sentence(work_id):
    spec = CATALOGUE[work_id]
    assert isinstance(spec, ImportSpec)
    assert spec.work_id == work_id
    assert spec.licence.rstrip().endswith(".")
    assert spec.licence.count(". ") == 0, "one sentence, not two"
    assert spec.licence_url.startswith("https://")
    assert spec.source_url.startswith("https://")


def test_the_blessed_texts_point_at_a_usfm_archive():
    for work_id in ("web", "bsb"):
        assert CATALOGUE[work_id].archive.endswith("_usfm.zip")
