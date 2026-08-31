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
from videomaker.providers.errors import ProviderConfigError
from videomaker.providers.mock import MockTTS
from videomaker.providers.tts.kokoro_onnx import (
    UNKNOWN_GENDER,
    UNKNOWN_LANGUAGE,
    describe_voice,
)
from videomaker.web.voices import FALLBACK_VOICES, available_voices, clear_voice_cache


@pytest.fixture(autouse=True)
def _no_cached_catalogue():
    """`available_voices` memoises success; no test may inherit another's answer."""
    clear_voice_cache()
    yield
    clear_voice_cache()


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
    monkeypatch.setattr(
        MockTTS, "voices", lambda self: ["zf_xiaoni", "af_heart", "bm_george", "am_adam"]
    )

    options = available_voices(settings)

    assert [group.language for group in options.groups] == [
        "American English",
        "British English",
        "Chinese",
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
