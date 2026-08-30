import pytest

from videomaker.config import Settings, load_settings
from videomaker.providers import get_provider, register, resolve_chain
from videomaker.providers.errors import ProviderConfigError


def test_register_and_retrieve():
    @register("llm", "dummy_for_test")
    class Dummy:
        def __init__(self, settings):
            self.settings = settings

    got = get_provider("llm", "dummy_for_test", Settings())
    assert isinstance(got, Dummy)


def test_unknown_provider_raises_config_error_listing_known_names():
    with pytest.raises(ProviderConfigError) as exc:
        get_provider("llm", "nope", Settings())
    assert "nope" in str(exc.value)


def test_default_chains():
    s = Settings()
    assert resolve_chain("llm", s) == ["groq", "gemini"]
    assert resolve_chain("stock", s) == ["pexels"]


def test_config_yaml_overrides_chain(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("providers:\n  llm: [gemini, groq]\n")
    s = load_settings(cfg)
    assert resolve_chain("llm", s) == ["gemini", "groq"]


def test_registering_same_name_twice_raises():
    @register("llm", "dupe_test")
    class A:
        def __init__(self, settings): ...

    with pytest.raises(ProviderConfigError):

        @register("llm", "dupe_test")
        class B:
            def __init__(self, settings): ...
