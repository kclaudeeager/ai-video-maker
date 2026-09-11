"""The importer, and the licence gate it exists to enforce.

`test_a_spec_without_a_licence_writes_nothing` is the one that matters: the rule
in `docs/multimodal-reader-design.md` §5 is only real if a refused import leaves
no directory behind. It bypasses `ImportSpec`'s own validation with
`model_construct` so it reaches `import_work`'s gate directly — the one a future
caller building specs some other way would hit.
"""

import json
import shutil
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from videomaker.cli import app
from videomaker.corpus import importer
from videomaker.corpus.importer import ImportSpec, import_work, list_works, read_work, work_dir
from videomaker.corpus.models import UnitText, WorkRef
from videomaker.corpus.usfm import iter_chapters, normalise_markup, parse_usfm

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "usfm"


@pytest.fixture(autouse=True)
def _own_notice(tmp_path, monkeypatch):
    """`NOTICE_PATH` is cwd-relative and pytest's cwd is the repository root, so
    without this every import here would append the fixture to the real file."""
    monkeypatch.setattr(importer, "NOTICE_PATH", tmp_path / "NOTICE.md")
FOOTNOTE = "FOOTNOTE-MARKER"
CROSSREF = "CROSSREF-MARKER"
HEADING = "A Heading That Must Not Be Read Aloud"


def spec(**overrides) -> ImportSpec:
    return ImportSpec(
        **{
            "work_id": "fixture",
            "title": "Longhand Test Fixture",
            "language": "en",
            "licence": "AGPL-3.0-only; invented text.",
            "licence_url": "https://www.gnu.org/licenses/agpl-3.0.html",
            "source_url": "https://example.invalid/fixture",
            "archive": str(FIXTURE_DIR),
            **overrides,
        }
    )


def unit_texts(root: Path) -> list[str]:
    files = sorted((work_dir(root, "fixture") / "units").glob("*/*.json"))
    return [v["text"] for f in files for v in json.loads(f.read_text())["verses"]]


# ------------------------------------------------------------------- the layout


def test_the_fixture_imports_to_the_documented_tree(tmp_path):
    work = import_work(spec(), tmp_path)
    root = work_dir(tmp_path, "fixture")
    assert sorted(p.relative_to(root).as_posix() for p in root.rglob("*.json")) == [
        "units/GEN/001.json",
        "units/GEN/002.json",
        "units/JHN/001.json",
        "units/JHN/002.json",
        "units/JHN/003.json",
        "units/MRK/001.json",
    ]
    assert (root / "source" / "44-JHN.usfm").read_text() == (
        FIXTURE_DIR / "44-JHN.usfm"
    ).read_text()
    assert (root / "derived").is_dir()
    assert read_work(root) == work
    assert WorkRef.model_validate(yaml.safe_load((root / "work.yaml").read_text())) == work


def test_a_unit_file_is_a_unit_text_addressed_by_its_own_key(tmp_path):
    import_work(spec(), tmp_path)
    unit = UnitText.model_validate_json(
        (work_dir(tmp_path, "fixture") / "units" / "JHN" / "003.json").read_text()
    )
    assert unit.ref.key() == "fixture/JHN/003"
    assert unit.title == "John 3"
    assert [v.number for v in unit.verses] == [1, 2, 3]


def test_list_works_reports_the_chapter_count(tmp_path):
    assert list_works(tmp_path) == []
    work = import_work(spec(), tmp_path)
    assert list_works(tmp_path) == [(work, 6)]


# ------------------------------------------------------------- the licence gate


def test_a_spec_without_a_licence_does_not_validate():
    with pytest.raises(ValidationError):
        spec(licence="")
    with pytest.raises(ValidationError):
        spec(licence_url="")
    with pytest.raises(ValidationError):
        spec(source_url="")


def test_a_spec_without_a_licence_writes_nothing(tmp_path):
    unlicensed = ImportSpec.model_construct(**{**spec().model_dump(), "licence": ""})
    with pytest.raises(ValidationError):
        import_work(unlicensed, tmp_path)
    assert not work_dir(tmp_path, "fixture").exists()
    assert not (tmp_path / "library").exists()


def test_a_spec_without_an_archive_is_refused_before_anything_is_written(tmp_path):
    with pytest.raises(ValueError, match="--from"):
        import_work(spec(archive=None), tmp_path)
    assert not (tmp_path / "library").exists()


# --------------------------------------------------------- what reaches the text


def test_apparatus_stays_in_source_and_reaches_no_verse(tmp_path):
    import_work(spec(), tmp_path)
    source = (work_dir(tmp_path, "fixture") / "source" / "44-JHN.usfm").read_text()
    texts = unit_texts(tmp_path)
    for apparatus in (FOOTNOTE, CROSSREF, HEADING):
        assert apparatus in source
        assert not any(apparatus in text for text in texts)
    # ...while a character marker loses its tags and keeps its word.
    assert any("The Narrator was invited" in text for text in texts)
    assert not any("\\" in text for text in texts)


def test_parse_usfm_reads_one_chapter_and_refuses_a_book():
    book = (FIXTURE_DIR / "44-JHN.usfm").read_text()
    with pytest.raises(ValueError, match="one chapter"):
        parse_usfm(book)
    assert parse_usfm("\\id JHN\n\\mt A title only\n") is None
    unit = parse_usfm("\\id GEN\n\\c 4\n\\p\n\\v 1-2 A bridged verse.\n\\v 3\n\\v 4 Four.\n")
    assert unit is not None
    assert unit.title == "Genesis 4"
    # A bridge files under its first number; an empty verse is absent, not blank.
    assert [(v.number, v.text) for v in unit.verses] == [(1, "A bridged verse."), (4, "Four.")]


def test_verse_text_is_whitespace_normalised():
    (unit,) = iter_chapters("\\id GEN\n\\c 1\n\\p\n\\v 1 First line\n\\q1 second   line.\n")
    assert unit.verses[0].text == "First line second line."


# ---------------------------------------------------------------- re-importing


def test_reimporting_replaces_source_and_units_and_keeps_derived(tmp_path):
    import_work(spec(), tmp_path)
    root = work_dir(tmp_path, "fixture")
    kept = root / "derived" / "brief" / "abc.json"
    kept.parent.mkdir(parents=True)
    kept.write_text("{}")
    stale_unit = root / "units" / "REV" / "001.json"
    stale_unit.parent.mkdir(parents=True)
    stale_unit.write_text("{}")
    (root / "source" / "stray.usfm").write_text("\\id REV\n")

    import_work(spec(), tmp_path)

    assert kept.read_text() == "{}"
    assert not stale_unit.exists()
    assert not (root / "source" / "stray.usfm").exists()
    assert not (root / importer.STAGING_DIRNAME).exists()


def test_a_local_zip_imports_the_same_as_a_directory(tmp_path):
    archive = shutil.make_archive(str(tmp_path / "usfm"), "zip", FIXTURE_DIR)
    import_work(spec(archive=archive), tmp_path / "from-zip")
    import_work(spec(), tmp_path / "from-dir")
    assert unit_texts(tmp_path / "from-zip") == unit_texts(tmp_path / "from-dir")


# ------------------------------------------------------------------- NOTICE.md


def test_the_work_is_noted_once_under_bundled_texts(tmp_path):
    notice = tmp_path / "NOTICE.md"
    notice.write_text("# Third-party notices\n\n- **something** — MIT.\n")
    import_work(spec(), tmp_path)
    import_work(spec(), tmp_path)
    text = notice.read_text()
    assert text.count(importer.NOTICE_SECTION) == 1
    assert text.count("(`fixture`,") == 1
    assert "AGPL-3.0-only; invented text." in text


def test_no_notice_file_is_created_where_there_is_none(tmp_path):
    import_work(spec(), tmp_path)
    assert not (tmp_path / "NOTICE.md").exists()


# ------------------------------------------------------------------------- CLI

runner = CliRunner()


@pytest.fixture
def cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_library_import_and_list_from_the_cli(cwd):
    empty = runner.invoke(app, ["library", "list"])
    assert empty.exit_code == 0
    assert "empty" in empty.output

    result = runner.invoke(app, ["library", "import", "fixture", "--from", str(FIXTURE_DIR)])
    assert result.exit_code == 0, result.output
    assert (cwd / "workspace" / "library" / "fixture" / "work.yaml").is_file()

    listed = runner.invoke(app, ["library", "list"])
    assert listed.exit_code == 0
    assert "fixture" in listed.output
    assert "AGPL" in listed.output


def test_library_import_of_an_unknown_work_fails_naming_the_catalogue(cwd):
    result = runner.invoke(app, ["library", "import", "nope"])
    assert result.exit_code == 1
    assert "nope" in result.output
    assert "web" in result.output
    assert not (cwd / "workspace").exists()


def test_the_cli_has_no_force_flag():
    result = runner.invoke(app, ["library", "import", "--help"])
    assert "--force" not in result.output


# --------------------------------------------- markup the grammar cannot read
#
# Both of these come from the Berean Standard Bible, where they are not rare:
# measured 2026-09-10, 53 of its 66 books raise `IndexError` out of
# `usfm_grammar` on the first and the remaining six on the second, so without
# `normalise_markup` the importer produces an empty library from a real Bible.


def test_a_words_of_jesus_span_straddling_a_verse_keeps_every_word():
    """`\\wj` opens before `\\v 2` and closes after it — malformed USFM, and what
    defeats the grammar's error recovery. The tags carry formatting this importer
    does not keep; the words inside them are ordinary verse text."""
    chapters = list(iter_chapters((FIXTURE_DIR / "41-MRK.usfm").read_text()))

    (chapter,) = chapters
    assert [verse.number for verse in chapter.verses] == [1, 2, 3]
    assert "These are quoted words that begin inside a span," in chapter.verses[1].text
    assert "and end inside another." in chapter.verses[1].text
    assert "\\wj" not in chapter.plain


def test_an_inline_ref_keeps_its_display_text_and_loses_its_target():
    text = normalise_markup(r"see \ref Luke 24:36-49|LUK 24:36-49\ref* now")

    assert text == "see Luke 24:36-49 now"
    assert "|" not in text


def test_a_wordlist_entry_keeps_its_word_and_loses_its_lexicon():
    """`\\w word|strong="G4314"\\w*` is a word plus apparatus. The World English
    Bible has 645,747 of them; the word is the text, the attributes are not."""
    text = normalise_markup(r'\w to|strong="G4314"\w* \w him|strong="G3588"\w*')

    assert text == "to him"
    assert "strong" not in text


def test_a_nested_wordlist_entry_inside_words_of_jesus_still_parses():
    """The construct that killed Matthew, Mark, Luke, John and Revelation.

    Stripping `\\wj` leaves every `\\+w` inside it with nothing to nest in, which
    is invalid USFM and crashes `usj_generator.node_2_usj_char` with an
    `IndexError` that `ignore_errors=True` does not catch. Measured on the WEB
    archive of 2026-09-11: 5 of 68 files, and precisely the books where Jesus
    speaks.
    """
    book = (
        "\\id MAT\n\\c 3\n\\p\n"
        r'\v 15 \w But|strong="G1161"\w* Jesus said, '
        r'\wj \u201cAllow \+w it|strong="G1161"\+w* \+w now|strong="G3568"\+w*.\wj*'
        "\n"
    ).replace(r"\u201c", "\u201c")

    (chapter,) = list(iter_chapters(book))

    assert chapter.verses[0].number == 15
    assert "But Jesus said," in chapter.verses[0].text
    assert "Allow it now." in chapter.verses[0].text


def test_a_hebrew_wordlist_entry_goes_the_same_way():
    assert normalise_markup(r'\wh Yahweh|strong="H3068"\wh*') == "Yahweh"


def test_a_wordlist_entry_without_attributes_keeps_its_word():
    assert normalise_markup(r"\w plain\w*") == "plain"


def test_the_normalisation_leaves_ordinary_usfm_alone():
    original = "\\c 1\n\\p\n\\v 1 Plain words with \\nd Lord\\nd* in them.\n"
    assert normalise_markup(original) == original


def test_the_cross_reference_line_still_reaches_no_verse():
    chapters = list(iter_chapters((FIXTURE_DIR / "41-MRK.usfm").read_text()))

    spoken = " ".join(verse.text for chapter in chapters for verse in chapter.verses)
    assert "Matthew 4:1-17" not in spoken, "an \\r line is apparatus, not the text"
    assert "FIXTURE-FOOTNOTE" not in spoken

