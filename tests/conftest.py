"""Fixtures shared by every test module.

The one thing every test needs is isolation from the machine it runs on. The runner
keeps its response cache and quota ledger in a single user-wide directory
(`runner.USER_CACHE_DIR`, `~/.cache/ai-video-maker`), so a test that forgot to
redirect it would read the developer's real cached provider answers — passing for
the wrong reason — or seed that cache with mock ones. Redirecting it once for the
whole session means no test can reach it, even by omission; modules that want their
own throwaway cache (`tests/unit/test_cli_m1.py`) still override it per test, and
the session value is restored afterwards.
"""

import pytest

from videomaker import runner as runner_module


@pytest.fixture(autouse=True, scope="session")
def _isolate_user_cache(tmp_path_factory):
    """Point the user-wide response cache and quota ledger at a throwaway directory."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(runner_module, "USER_CACHE_DIR", tmp_path_factory.mktemp("user_cache"))
        yield
