"""The voice menu the create form renders: what the TTS chain has, by language.

Two things shape this module.

**It must never take the create form down.** `KokoroTTS.voices()` raises
`ProviderConfigError` when the model weights are missing — the state of every
fresh clone, and of CI, which installs without the `ml` extra. A `<select>` built
straight from `voices()` would turn `GET /` into a 500 for anyone who has not run
`videomaker setup` yet. So every provider failure lands on `FALLBACK_VOICES`, a
short static list that always contains the form's own default, and the page says
why the menu is short instead of pretending it is complete.

**Asking is expensive.** Kokoro answers `voices()` by loading a 310 MB ONNX graph
(M0 finding 2), and `get_provider` builds a fresh instance per call, so an
uncached lookup would pay that on every render of the project list. A successful
answer is therefore memoised for the life of the process. Failures are *not*
cached: they are cheap to redo (a missing-file check), and caching one would mean
a server started before `videomaker setup` kept showing the short list forever.
"""

from dataclasses import dataclass

from videomaker.config import Settings
from videomaker.providers import get_provider, resolve_chain
from videomaker.providers.errors import ProviderError
from videomaker.providers.tts.kokoro_onnx import (
    UNKNOWN_LANGUAGE,
    VoiceInfo,
    describe_voice,
)

#: Offered when no provider in the chain can say what it has. Kokoro ships these
#: under any install, so they are safe to name; the first one is the create
#: form's own `DEFAULT_VOICE`, which is what makes the fallback menu usable
#: rather than merely non-empty.
FALLBACK_VOICES: tuple[str, ...] = (
    "af_heart",
    "af_sky",
    "am_adam",
    "bf_emma",
    "bm_george",
)

#: Shown under a fallback menu. It names the fix, because the fix is one command.
FALLBACK_NOTE = (
    "Showing a short built-in list: the voice catalogue could not be read "
    "({reason}). Run `videomaker setup` to install the Kokoro voices."
)

#: Successful catalogues, keyed by what could change the answer.
_CACHE: dict[tuple[str, ...], list[str]] = {}


@dataclass(frozen=True)
class VoiceGroup:
    """One `<optgroup>`: a language, and the voices that speak it."""

    language: str
    voices: list[VoiceInfo]


@dataclass(frozen=True)
class VoiceOptions:
    """The whole menu, plus whether it is the real thing."""

    groups: list[VoiceGroup]
    #: True when no provider could be asked, so `groups` is the static list.
    fallback: bool = False
    #: Why the menu is short. Empty whenever `fallback` is false.
    note: str = ""

    @property
    def ids(self) -> list[str]:
        """Every offered voice id, in menu order — what the template checks against."""
        return [voice.id for group in self.groups for voice in group.voices]


def clear_voice_cache() -> None:
    """Forget memoised catalogues. For tests, and for a process that ran `setup`."""
    _CACHE.clear()


def _cache_key(settings: Settings) -> tuple[str, ...]:
    """What a catalogue depends on: which providers are asked, and from where."""
    return (*resolve_chain("tts", settings), str(settings.models_dir))


def _query_chain(settings: Settings) -> list[str]:
    """The first TTS provider in the chain that can answer, or raise.

    Both raise sites are treated alike — a provider that cannot be built and one
    that builds but has no weights are the same thing to a form that just wants a
    list — so the walk simply moves on and only the last complaint survives.
    """
    names = resolve_chain("tts", settings)
    if not names:
        raise ProviderError("no tts provider is configured")
    problems: list[str] = []
    for name in names:
        try:
            provider = get_provider("tts", name, settings)
            return sorted(provider.voices())
        except ProviderError as exc:
            problems.append(f"{name}: {exc}")
    raise ProviderError("; ".join(problems))


def _grouped(voice_ids: list[str]) -> list[VoiceGroup]:
    """Voices by language, languages A-Z with `Other` last so it cannot lead."""
    by_language: dict[str, list[VoiceInfo]] = {}
    for voice_id in voice_ids:
        info = describe_voice(voice_id)
        by_language.setdefault(info.language, []).append(info)
    order = sorted(by_language, key=lambda name: (name == UNKNOWN_LANGUAGE, name))
    return [VoiceGroup(language=name, voices=by_language[name]) for name in order]


def available_voices(settings: Settings) -> VoiceOptions:
    """The voice menu for `settings`' TTS chain, grouped by language.

    Never raises: a chain that cannot answer yields the static fallback and a note
    explaining it, because the caller is a page that has to render either way.
    """
    key = _cache_key(settings)
    cached = _CACHE.get(key)
    if cached is not None:
        return VoiceOptions(groups=_grouped(cached))
    try:
        voice_ids = _query_chain(settings)
    except ProviderError as exc:
        return VoiceOptions(
            groups=_grouped(sorted(FALLBACK_VOICES)),
            fallback=True,
            note=FALLBACK_NOTE.format(reason=exc),
        )
    _CACHE[key] = voice_ids
    return VoiceOptions(groups=_grouped(voice_ids))
