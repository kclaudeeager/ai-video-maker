"""Fixtures shared by every test module.

The one thing every test needs is isolation from the machine it runs on. The runner
keeps its response cache and quota ledger in a single user-wide directory
(`runner.USER_CACHE_DIR`, `~/.cache/ai-video-maker`), so a test that forgot to
redirect it would read the developer's real cached provider answers — passing for
the wrong reason — or seed that cache with mock ones. Redirecting it once for the
whole session means no test can reach it, even by omission; modules that want their
own throwaway cache (`tests/unit/test_cli_m1.py`) still override it per test, and
the session value is restored afterwards.

The music index (`audio.DEFAULT_INDEX_PATH`) lives in the same user-wide directory
and is redirected for the same reason: `videomaker music scan` writes it, and a
test that forgot would clobber the developer's real one.

The library itself is redirected too, and that one is about *results* rather than
tidiness. `Settings.music_dir` defaults to `assets/music` relative to the working
directory, and `.gitignore` keeps that folder out of the repository — so it is empty
on CI and may hold anything at all on the machine of whoever is working on this. A
render test that did not name its own music directory would put the developer's own
tracks under its output and measure something nobody else can reproduce. Setting the
environment variables rather than patching means any `Settings()` picks it up, while
a test that passes `music_dir=` explicitly still wins.
"""

import pytest

from videomaker import audio as audio_module
from videomaker import runner as runner_module


@pytest.fixture(autouse=True, scope="session")
def _isolate_user_cache(tmp_path_factory):
    """Point the caches and the audio library at throwaway paths."""
    cache_dir = tmp_path_factory.mktemp("user_cache")
    library_dir = tmp_path_factory.mktemp("empty_library")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(runner_module, "USER_CACHE_DIR", cache_dir)
        patch.setattr(audio_module, "DEFAULT_INDEX_PATH", cache_dir / audio_module.INDEX_FILENAME)
        patch.setenv("MUSIC_DIR", str(library_dir / "music"))
        patch.setenv("SFX_DIR", str(library_dir / "sfx"))
        yield
