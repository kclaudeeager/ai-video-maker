"""The corpus models, the `CorpusProvider` contract, and the offline mock library.

Two of these are about a rule rather than a type. `test_a_work_without_a_licence_is_not_a_work`
is `docs/multimodal-reader-design.md` §5 in executable form — the importer's gate is
built on this validator, so if it stops raising, the gate stops existing. And
`test_provider_override_covers_the_corpus` is what keeps `--providers mock` total:
one kind left pointing at the real chain would send a reader test looking for a
library on the developer's disk.
"""

import pytest
from pydantic import ValidationError

from videomaker.config import Settings
from videomaker.corpus.models import UnitRef, UnitText, Verse, Versification, WorkRef
from videomaker.providers import get_provider
from videomaker.providers.base import CorpusProvider
from videomaker.runner import PROVIDER_KINDS, provider_override

WHOLE_CHAPTER = UnitRef(work_id="web", book="JHN", chapter=3)
VERSE_RANGE = UnitRef(work_id="web", book="JHN", chapter=3, verses=(16, 18))


def _work(**overrides) -> dict:
    return {
        "id": "web",
        "title": "World English Bible",
        "language": "en",
        "licence": "Public domain.",
        "licence_url": "https://ebible.org/web/",
        "source_url": "https://ebible.org/",
        **overrides,
    }


# ------------------------------------------------------------------- references


@pytest.mark.parametrize("ref", [WHOLE_CHAPTER, VERSE_RANGE])
def test_a_key_round_trips_back_to_the_same_reference(ref):
    assert UnitRef.parse(ref.key()) == ref


def test_the_key_shapes_are_the_documented_ones():
    assert WHOLE_CHAPTER.key() == "web/JHN/003"
    assert VERSE_RANGE.key() == "web/JHN/003.16-18"


@pytest.mark.parametrize(
    "key",
    [
        "web/JHN",
        "web/JHN/003.16",
        "web/John/3",
        "web/../003",
        "web/JHN/003/16",
        "",
    ],
)
def test_a_key_that_is_not_a_key_raises_naming_the_input(key):
    with pytest.raises(ValueError, match="not a unit key"):
        UnitRef.parse(key)


def test_a_book_code_that_is_not_three_uppercase_characters_is_refused():
    # The code becomes a directory name and a URL segment; the shape is refused
    # here so neither the importer nor the web layer has to think about it.
    with pytest.raises(ValidationError):
        UnitRef(work_id="web", book="../", chapter=1)


# ------------------------------------------------------------------ the licence


def test_a_work_without_a_licence_is_not_a_work():
    with pytest.raises(ValidationError):
        WorkRef(**_work(licence=""))


def test_a_work_with_a_stateable_licence_validates():
    work = WorkRef(**_work())
    assert work.versification is Versification.KJV


# ----------------------------------------------------------------- passage text


def test_plain_text_carries_no_verse_numbers():
    unit = UnitText(
        ref=WHOLE_CHAPTER,
        title="John 3",
        verses=[Verse(number=n, text=f"Sentence {'word ' * n}here.") for n in (1, 2, 3)],
    )
    # The numbers are 1-3 and no verse text contains a digit, so any digit in
    # `plain` came from a verse number — which a narrator would read aloud.
    assert not any(character.isdigit() for character in unit.plain)
    assert unit.plain.startswith("Sentence")
    assert unit.word_count == len(unit.plain.split())


# ---------------------------------------------------------------- the mock work


@pytest.fixture
def corpus() -> CorpusProvider:
    provider = get_provider("corpus", "mock", Settings())
    assert isinstance(provider, CorpusProvider)
    return provider


def test_the_mock_library_serves_one_work_with_a_licence(corpus):
    (work,) = corpus.works()
    assert work.licence
    assert work.language == "en"


def test_the_mock_outline_addresses_units_that_resolve(corpus):
    (work,) = corpus.works()
    outline = corpus.outline(work.id)
    assert len(outline) > 1
    for ref in outline:
        unit = corpus.unit(ref)
        assert unit.ref == ref
        assert unit.verses


def test_the_mock_library_raises_for_a_chapter_it_does_not_have(corpus):
    missing = UnitRef(work_id="mock", book="JHN", chapter=99)
    with pytest.raises(KeyError, match="mock/JHN/099"):
        corpus.unit(missing)


# ------------------------------------------------------------ provider override


def test_provider_override_covers_the_corpus():
    assert "corpus" in PROVIDER_KINDS
    overridden = provider_override(Settings(), "mock")
    assert overridden.provider_chains["corpus"] == ["mock"]
