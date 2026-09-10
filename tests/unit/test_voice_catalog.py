"""The voice catalogue: naming convention in, grouped menu out.

Two halves, deliberately kept apart:

* `describe_voice` is pure string work living with the provider that owns the
  convention, so it needs neither the `ml` extra nor the model weights.
* `available_voices` asks the configured TTS chain what it actually has.

**The fallback is the load-bearing part.** `KokoroTTS.voices()` raises
`ProviderConfigError` whenever the weights are absent, which is the state of a
fresh clone and of CI (installed without `ml`). A create form that 500s there
would be worse than a form with a short menu, so the failure is simulated here
rather than inferred from whether this machine happens to have the extra.
"""

import pytest

from videomaker.config import Settings
from videomaker.media import fonts
from videomaker.providers.errors import ProviderConfigError
from videomaker.providers.mock import MockTTS
from videomaker.providers.tts.kokoro_onnx import (
    UNKNOWN_GENDER,
    UNKNOWN_LANGUAGE,
    describe_voice,
    espeak_language,
)
from videomaker.web.voices import FALLBACK_VOICES, available_voices, clear_voice_cache


@pytest.fixture(autouse=True)
def _no_cached_catalogue():
    """Both halves memoise: the catalogue, and the fontconfig charset dump."""
    clear_voice_cache()
    fonts.clear_font_cache()
    yield
    clear_voice_cache()
    fonts.clear_font_cache()


@pytest.fixture
def settings(tmp_path) -> Settings:
    """A mock-only TTS chain: a real `voices()` call that needs no weights."""
    return Settings(workspace_dir=tmp_path / "workspace").model_copy(
        update={"provider_chains": {"tts": ["mock"]}}
    )


# ------------------------------------------------------------------ describe_voice


@pytest.mark.parametrize(
    ("voice_id", "language", "gender", "display_name"),
    [
        ("af_heart", "American English", "female", "Heart"),
        ("am_michael", "American English", "male", "Michael"),
        ("bf_emma", "British English", "female", "Emma"),
        ("bm_george", "British English", "male", "George"),
        ("ef_dora", "Spanish", "female", "Dora"),
        ("em_alex", "Spanish", "male", "Alex"),
        ("ff_siwis", "French", "female", "Siwis"),
        ("hf_alpha", "Hindi", "female", "Alpha"),
        ("hm_omega", "Hindi", "male", "Omega"),
        ("if_sara", "Italian", "female", "Sara"),
        ("im_nicola", "Italian", "male", "Nicola"),
        ("jf_gongitsune", "Japanese", "female", "Gongitsune"),
        ("jm_kumo", "Japanese", "male", "Kumo"),
        ("pf_dora", "Portuguese", "female", "Dora"),
        ("pm_santa", "Portuguese", "male", "Santa"),
        ("zf_xiaoxiao", "Chinese", "female", "Xiaoxiao"),
        ("zm_yunyang", "Chinese", "male", "Yunyang"),
    ],
)
def test_describe_voice_reads_the_kokoro_naming_convention(
    voice_id: str, language: str, gender: str, display_name: str
) -> None:
    info = describe_voice(voice_id)

    assert info.id == voice_id
    assert info.language == language
    assert info.gender == gender
    assert info.display_name == display_name


def test_an_unknown_language_prefix_degrades_instead_of_raising() -> None:
    """The model file can gain voices this table has never heard of."""
    info = describe_voice("qf_luna")

    assert info.id == "qf_luna"
    assert info.language == UNKNOWN_LANGUAGE
    # The gender letter is still the convention's, so half-known beats unknown.
    assert info.gender == "female"
    assert info.display_name == "Luna"


def test_a_voice_id_shaped_like_nothing_at_all_still_describes() -> None:
    info = describe_voice("robot")

    assert info.id == "robot"
    assert info.language == UNKNOWN_LANGUAGE
    assert info.gender == UNKNOWN_GENDER
    assert info.display_name == "Robot"


# ---------------------------------------------------------------- available_voices


def test_available_voices_reports_what_the_provider_really_has(settings) -> None:
    options = available_voices(settings)

    assert not options.fallback
    assert not options.note
    assert options.ids == sorted(MockTTS(settings).voices())


def test_available_voices_groups_by_language(settings, monkeypatch) -> None:
    """Chinese is dropped rather than grouped — see the language-binding tests."""
    monkeypatch.setattr(
        MockTTS, "voices", lambda self: ["zf_xiaoni", "af_heart", "bm_george", "am_adam"]
    )

    options = available_voices(settings)

    assert [group.language for group in options.groups] == [
        "American English",
        "British English",
    ]
    assert [voice.id for voice in options.groups[0].voices] == ["af_heart", "am_adam"]


def test_available_voices_falls_back_when_the_provider_has_no_weights(
    settings, monkeypatch
) -> None:
    """The fresh-clone case: `voices()` raises, the form must still render."""

    def no_weights(self):
        raise ProviderConfigError("missing Kokoro model files; run `videomaker setup`")

    monkeypatch.setattr(MockTTS, "voices", no_weights)

    options = available_voices(settings)

    assert options.fallback
    assert options.ids == sorted(FALLBACK_VOICES)
    assert "af_heart" in options.ids, "the form's default must always be offerable"
    assert options.groups, "a fallback list is still a grouped list"
    assert "videomaker setup" in options.note


def test_available_voices_falls_back_when_no_provider_can_be_built(settings) -> None:
    """The other raise site: the chain names a provider that does not exist."""
    broken = settings.model_copy(update={"provider_chains": {"tts": ["nonesuch"]}})

    options = available_voices(broken)

    assert options.fallback
    assert "af_heart" in options.ids


def test_a_working_catalogue_is_only_read_once(settings, monkeypatch) -> None:
    """Kokoro loads a 310 MB graph to answer this; a page view must not pay twice."""
    calls: list[int] = []

    def counted(self):
        calls.append(1)
        return ["af_heart"]

    monkeypatch.setattr(MockTTS, "voices", counted)

    available_voices(settings)
    available_voices(settings)

    assert len(calls) == 1


# --------------------------------------------------------- the language binding
#
# M3 Task 21: the voice prefix *is* the language, so nothing may select the two
# independently. These lock the prefix -> ISO 639-1 half of that, and the fact
# that a language the chain was measured to get wrong is not in the menu at all.


@pytest.mark.parametrize(
    ("voice_id", "code"),
    [
        ("af_heart", "en"),
        ("bm_george", "en"),
        ("ef_dora", "es"),
        ("ff_siwis", "fr"),
        ("hf_alpha", "hi"),
        ("if_sara", "it"),
        ("jf_alpha", "ja"),
        ("pf_dora", "pt"),
        ("zf_xiaoxiao", "zh"),
    ],
)
def test_describe_voice_reads_the_language_code_the_pipeline_speaks(
    voice_id: str, code: str
) -> None:
    """`Project.language` is derived from this, and whisper is handed it as-is."""
    assert describe_voice(voice_id).code == code


def test_an_unknown_prefix_has_no_language_code_rather_than_a_wrong_one() -> None:
    assert describe_voice("qf_luna").code == ""
    assert describe_voice("robot").code == ""


def test_kokoro_is_asked_for_the_dialect_the_voice_actually_speaks() -> None:
    """One ISO code in, espeak's own code out — and `b` voices are British."""
    assert espeak_language("en", "af_heart") == "en-us"
    assert espeak_language("en", "bm_george") == "en-gb"
    assert espeak_language("fr", "ff_siwis") == "fr-fr"
    assert espeak_language("pt", "pf_dora") == "pt-br"


def test_an_unregistered_language_is_passed_through_untouched() -> None:
    """Kokoro may learn a language before this table does; do not mangle it."""
    assert espeak_language("ko", "kf_someone") == "ko"


def test_the_menu_withholds_a_language_the_chain_gets_wrong(settings, monkeypatch) -> None:
    """Japanese renders fine on a Noto box; the *audio* is what was measured wrong."""
    monkeypatch.setattr(
        MockTTS, "voices", lambda self: ["af_heart", "jf_alpha", "zf_xiaoni", "ef_dora"]
    )

    options = available_voices(settings)

    assert options.ids == ["af_heart", "ef_dora"]
    assert [group.language for group in options.groups] == ["American English", "Spanish"]
    assert options.withheld == ("Japanese", "Chinese")
    assert "language-support" in options.withheld_note


def test_a_voice_whose_language_cannot_be_read_is_still_offered(settings, monkeypatch) -> None:
    """A future Kokoro voice must not vanish just because this table is behind."""
    monkeypatch.setattr(MockTTS, "voices", lambda self: ["af_heart", "qf_luna"])

    assert available_voices(settings).ids == ["af_heart", "qf_luna"]


def test_the_menu_drops_a_language_this_machine_cannot_draw(settings, monkeypatch) -> None:
    """The second gate: measured-good Spanish is useless with no accented glyphs."""
    monkeypatch.setattr(MockTTS, "voices", lambda self: ["af_heart", "ef_dora"])
    monkeypatch.setattr(fonts, "fontconfig_charsets", lambda: "20-7e\n2010-2027\n")
    fonts.clear_font_cache()

    options = available_voices(settings)

    assert options.ids == ["af_heart"]
    assert "Spanish" in options.withheld


def test_the_language_menu_lists_the_groups_the_voice_menu_has(settings, monkeypatch) -> None:
    """One choice, not two: the filter can only name a group that exists."""
    monkeypatch.setattr(MockTTS, "voices", lambda self: ["af_heart", "bm_george", "ef_dora"])

    options = available_voices(settings)

    assert options.language_labels == ["American English", "British English", "Spanish"]


# --------------------------------------------- the optional extra, when it is not there
#
# Every optional import in this package degrades to "provider unavailable" rather
# than an `ImportError`, because `ProviderConfigError` is in `ADVANCE_ON` and a
# bare `ModuleNotFoundError` is not: one advances the chain to the next provider,
# the other kills the run. `soundfile` was the one import that had no guard, and
# it sat at the *top* of `synthesize`, above the `kokoro_onnx` import — so on a
# clone without the extra it pre-empted the better message and the reader showed
# `No module named 'soundfile'`.


@pytest.fixture
def without(monkeypatch):
    """Make one optional module unimportable, whether or not it is installed."""

    def hide(name: str):
        import sys

        monkeypatch.setitem(sys.modules, name, None)

    return hide


def test_a_missing_soundfile_is_a_provider_error_naming_the_command(without, tmp_path):
    from videomaker.providers.tts.kokoro_onnx import EXTRA_HINT, KokoroTTS

    without("soundfile")
    tts = KokoroTTS(Settings(models_dir=tmp_path))

    with pytest.raises(ProviderConfigError) as exc:
        tts.synthesize(text="a line", voice="af_heart", out_path=tmp_path / "out.wav")

    assert "soundfile" in str(exc.value)
    assert EXTRA_HINT in str(exc.value)
    assert "--extra ml" in str(exc.value)


def test_a_missing_engine_is_the_same_kind_of_refusal(without, tmp_path):
    from videomaker.providers.tts.kokoro_onnx import EXTRA_HINT, KokoroTTS

    without("kokoro_onnx")
    tts = KokoroTTS(Settings(models_dir=tmp_path))

    with pytest.raises(ProviderConfigError) as exc:
        tts.voices()

    assert EXTRA_HINT in str(exc.value)


def test_a_missing_extra_advances_the_chain_rather_than_ending_the_run(without, tmp_path):
    """The reason the type matters. `ProviderConfigError` is in `ADVANCE_ON`; a
    `ModuleNotFoundError` is not, so an unguarded import turns a fallback into a
    dead run."""
    from videomaker.pipeline.base import ADVANCE_ON
    from videomaker.providers.tts.kokoro_onnx import KokoroTTS

    without("soundfile")
    tts = KokoroTTS(Settings(models_dir=tmp_path))

    with pytest.raises(ADVANCE_ON):
        tts.synthesize(text="a line", voice="af_heart", out_path=tmp_path / "out.wav")


@pytest.fixture
def half_installed(monkeypatch, tmp_path, without):
    """`kokoro_onnx` present and working, `soundfile` absent, weights on disk.

    Built rather than skipped, because this machine has neither half of the extra
    and the state under test is the one where exactly one half is there — which is
    what a partial `uv sync` leaves behind and what a user actually hit.
    """
    import sys
    import types

    from videomaker.providers.tts.kokoro_onnx import KOKORO_MODEL_FILE, KOKORO_VOICES_FILE

    for name in (KOKORO_MODEL_FILE, KOKORO_VOICES_FILE):
        (tmp_path / name).write_bytes(b"not really a model")

    module = types.ModuleType("kokoro_onnx")

    class _Kokoro:
        def __init__(self, *_args) -> None: ...

        def get_voices(self):
            return ["af_heart", "am_adam"]

    module.Kokoro = _Kokoro
    monkeypatch.setitem(sys.modules, "kokoro_onnx", module)
    without("soundfile")
    return Settings(workspace_dir=tmp_path, models_dir=tmp_path, provider_chains={"tts": ["kokoro"]})


def test_a_half_installed_extra_reports_no_voices_at_all(half_installed):
    """Listing must not succeed where synthesis would fail, or the reader offers
    Listen on a work it cannot speak."""
    from videomaker.providers.tts.kokoro_onnx import KokoroTTS

    tts = KokoroTTS(half_installed)

    with pytest.raises(ProviderConfigError, match="soundfile"):
        tts.voices()


def test_a_half_installed_extra_offers_no_listen_mode(half_installed):
    """The whole point of the check: the mode table degrades instead of breaking.

    With `voices()` answering happily this returned `{"en"}`, the reader drew a
    Listen tab, and pressing it failed after the fact — the state a user reported.
    """
    from videomaker.web.routes.library import spoken_languages

    assert spoken_languages(half_installed) == set()


def test_the_reader_offers_no_listen_when_no_voice_can_be_built(without, tmp_path):
    """The mode table degrades rather than breaking — §6 of the reader design."""
    from videomaker.web.routes.library import spoken_languages

    without("soundfile")
    without("kokoro_onnx")
    settings = Settings(
        workspace_dir=tmp_path, models_dir=tmp_path, provider_chains={"tts": ["kokoro"]}
    )

    assert spoken_languages(settings) == set()
