"""What the stack can actually do per language, and how that was measured.

Two independent gates, tested apart because they fail for different reasons:

* **the chain gate** — `videomaker.languages`, a recorded verdict per language
  from an end-to-end spot check (Kokoro synthesis, then faster-whisper back).
  A static table, because it records a measurement, not a machine's state.
* **the script gate** — `videomaker.media.fonts`, probed live against the fonts
  actually installed. libass falls back per glyph through fontconfig, so the
  question is never "does the styled font have this glyph" but "can *anything*
  installed draw it" — see `docs/language-support.md`.

The fontconfig probe is a subprocess, so every test that needs a determinate
answer goes through the one seam (`fonts.fontconfig_charsets`) rather than
hoping this machine has, or lacks, a given font.
"""

import pytest

from videomaker import languages
from videomaker.languages import LANGUAGES, OFFERED_CODES, Language
from videomaker.media import fonts

#: U+0378 is permanently unassigned in Unicode, so no font may claim it. It is
#: the one codepoint a coverage probe can be asked about with a known answer.
UNASSIGNED = "͸"


@pytest.fixture(autouse=True)
def _no_cached_coverage():
    """The fontconfig dump is memoised per process; no test may inherit another's."""
    fonts.clear_font_cache()
    yield
    fonts.clear_font_cache()


# ------------------------------------------------------------------- the registry


def test_every_language_carries_the_codes_its_two_consumers_need() -> None:
    """`code` goes to faster-whisper, `espeak` to Kokoro. Neither may be guessed."""
    for language in LANGUAGES.values():
        assert language.code, language
        assert language.espeak, language
        assert language.name, language
        assert language.script_sample, f"{language.code} must be probeable"


def test_only_the_measured_languages_are_offered() -> None:
    """The five that survived the round-trip spot check on 2026-08-31."""
    assert OFFERED_CODES == ("en", "es", "fr", "it", "pt")


def test_a_language_that_is_not_offered_says_why() -> None:
    """Refusing a language is a claim; an unexplained one cannot be argued with."""
    held_back = [lang for lang in LANGUAGES.values() if not lang.offered]

    assert {lang.code for lang in held_back} == {"hi", "ja", "zh"}
    for language in held_back:
        assert language.note, f"{language.code} is refused with no stated reason"


def test_an_offered_language_makes_no_excuses() -> None:
    for code in OFFERED_CODES:
        assert LANGUAGES[code].offered
        assert not LANGUAGES[code].note


def test_language_for_an_unknown_code_is_none() -> None:
    assert languages.language_for("kl") is None
    assert languages.language_for("") is None
    assert languages.language_for("es") == LANGUAGES["es"]


# -------------------------------------------------------------- the script probe


def test_nothing_installed_can_draw_an_unassigned_codepoint() -> None:
    """A live probe with a known answer: U+0378 is not, and cannot be, assigned."""
    if fonts.fontconfig_charsets() is None:
        pytest.skip("no fontconfig on this machine")

    assert fonts.undrawable(UNASSIGNED) == UNASSIGNED


def test_plain_ascii_is_drawable_wherever_there_are_fonts_at_all() -> None:
    if fonts.fontconfig_charsets() is None:
        pytest.skip("no fontconfig on this machine")

    assert fonts.undrawable("The quick brown fox") == ""


def test_undrawable_reports_each_missing_character_once_in_order(monkeypatch) -> None:
    monkeypatch.setattr(fonts, "fontconfig_charsets", lambda: "61-7a\n")

    assert fonts.undrawable("banana ñoño") == "ñ"


def test_a_space_is_never_reported_as_undrawable(monkeypatch) -> None:
    """Whitespace is laid out, not drawn; a font missing U+0020 breaks nothing."""
    monkeypatch.setattr(fonts, "fontconfig_charsets", lambda: "61-7a\n")

    assert fonts.undrawable("a b\tc\nd") == ""


def test_coverage_is_the_union_over_every_installed_font(monkeypatch) -> None:
    """libass falls back per glyph, so two fonts between them are enough."""
    monkeypatch.setattr(fonts, "fontconfig_charsets", lambda: "61-7a\n4e2d\n")

    assert fonts.undrawable("abc中") == ""


def test_the_charset_dump_is_only_read_once(monkeypatch) -> None:
    """It is a 3 MB subprocess; the create form must not pay for it per request."""
    calls: list[int] = []

    def counted() -> str:
        calls.append(1)
        return "61-7a\n"

    monkeypatch.setattr(fonts, "fontconfig_charsets", counted)
    fonts.undrawable("aaa")
    fonts.undrawable("bbb")

    assert len(calls) == 1


# ------------------------------------------------------- the two gates, combined


def test_script_checks_cover_every_registered_language(monkeypatch) -> None:
    monkeypatch.setattr(fonts, "fontconfig_charsets", lambda: "0-10ffff\n")

    checks = fonts.script_checks()

    assert [check.code for check in checks] == list(LANGUAGES)
    assert all(check.probed for check in checks)
    assert all(check.missing == "" for check in checks)


def test_a_language_whose_script_nothing_can_draw_is_not_renderable(monkeypatch) -> None:
    """Latin only installed: Spanish still renders, Hindi and Chinese do not."""
    monkeypatch.setattr(fonts, "fontconfig_charsets", lambda: "20-24f\n2010-2027\n")

    renderable = fonts.renderable_codes()

    assert "es" in renderable
    assert "fr" in renderable
    assert "hi" not in renderable
    assert "zh" not in renderable


def test_offered_languages_are_the_measured_ones_that_also_render(monkeypatch) -> None:
    """Both gates, and a language must pass both. Chinese renders here; it is
    still not offered, because the audio was measured wrong."""
    monkeypatch.setattr(fonts, "fontconfig_charsets", lambda: "0-10ffff\n")

    assert fonts.offerable_codes() == frozenset(OFFERED_CODES)


def test_a_language_drops_out_when_its_script_cannot_be_drawn(monkeypatch) -> None:
    """No accented Latin installed: Spanish is measured-good but unrenderable."""
    monkeypatch.setattr(fonts, "fontconfig_charsets", lambda: "20-7e\n2010-2027\n")

    offerable = fonts.offerable_codes()

    assert "en" in offerable
    assert "es" not in offerable


def test_an_unprobeable_machine_offers_the_measured_set_rather_than_nothing(
    monkeypatch,
) -> None:
    """No fontconfig: libass has no fallback either, so guessing helps nobody.
    Fail open and let `doctor` say the probe never ran."""
    monkeypatch.setattr(fonts, "fontconfig_charsets", lambda: None)

    assert fonts.offerable_codes() == frozenset(OFFERED_CODES)
    assert all(not check.probed for check in fonts.script_checks())
    assert all(check.missing == "" for check in fonts.script_checks())


# ------------------------------------------------------------ the caption family


def test_resolving_the_caption_family_reports_the_file_libass_would_load() -> None:
    if fonts.fontconfig_charsets() is None:
        pytest.skip("no fontconfig on this machine")

    resolved = fonts.resolve_family(fonts.CAPTION_FONT)

    assert resolved is not None
    assert resolved.family
    assert resolved.path
    assert resolved.exact, f"{fonts.CAPTION_FONT} is not installed; got {resolved.family}"


def test_a_family_nobody_has_resolves_to_a_substitute_that_is_not_exact() -> None:
    if fonts.fontconfig_charsets() is None:
        pytest.skip("no fontconfig on this machine")

    resolved = fonts.resolve_family("No Such Family At All")

    assert resolved is not None
    assert not resolved.exact


def test_the_language_registry_is_immutable() -> None:
    """It is a recorded measurement, not a runtime cache."""
    with pytest.raises(AttributeError):
        LANGUAGES["en"].offered = False  # type: ignore[misc]

    assert isinstance(LANGUAGES["en"], Language)
