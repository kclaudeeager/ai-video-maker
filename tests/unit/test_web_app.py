from fastapi.testclient import TestClient

from videomaker import __version__
from videomaker.config import Settings
from videomaker.web.app import create_app


def _client(tmp_path) -> TestClient:
    return TestClient(create_app(Settings(workspace_dir=tmp_path)))


def test_healthz(tmp_path):
    response = _client(tmp_path).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


def test_app_factory_isolates_state(tmp_path):
    a = create_app(Settings(workspace_dir=tmp_path / "a"))
    b = create_app(Settings(workspace_dir=tmp_path / "b"))
    assert a.state.store.workspace_dir != b.state.store.workspace_dir


def test_provider_override_reaches_app_state(tmp_path):
    app = create_app(Settings(workspace_dir=tmp_path), providers="mock")
    assert app.state.settings.provider_chains["llm"] == ["mock"]


def test_unknown_route_is_404(tmp_path):
    assert _client(tmp_path).get("/no-such-page").status_code == 404


# `serve` itself is not in the plan's test block; these guard the one thing the
# task calls out as security-relevant — what happens on a non-loopback bind.
#
# M6 changed the answer. It used to print three red warnings and serve anyway,
# which is the shape of every accidentally-public deployment: the warning scrolls
# past and the open port stays. It now refuses, and the password is what lifts the
# refusal. The gate itself is covered in `test_web_auth.py`.


def _invoke_serve(monkeypatch, host: str, password: str | None = None):
    """Run `serve` with uvicorn stubbed, returning (exit code, output, calls)."""
    import uvicorn
    from typer.testing import CliRunner

    from videomaker.cli import app as cli_app
    from videomaker.web.auth import PASSWORD_ENV

    if password is None:
        monkeypatch.delenv(PASSWORD_ENV, raising=False)
    else:
        monkeypatch.setenv(PASSWORD_ENV, password)

    calls: list[dict] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append(kw))
    result = CliRunner().invoke(cli_app, ["serve", "--host", host, "--port", "8123"])
    # rich hard-wraps at the terminal width; collapse it so asserts see one line.
    return result.exit_code, " ".join(result.output.split()), calls


def _run_serve(monkeypatch, host: str, password: str | None = None) -> str:
    code, output, calls = _invoke_serve(monkeypatch, host, password)
    assert code == 0, output
    assert calls == [{"host": host, "port": 8123}]
    return output


def test_serve_refuses_a_non_loopback_host_with_no_password(monkeypatch):
    """The whole point of M6's gate: it does not warn and serve anyway."""
    code, output, calls = _invoke_serve(monkeypatch, "0.0.0.0")

    assert code == 2, output
    assert calls == [], "uvicorn must never be reached"
    assert "REFUSED" in output
    assert "LONGHAND_PASSWORD" in output
    # A refusal that does not say how to lift it is just an obstacle.
    assert "token_urlsafe" in output


def test_serve_binds_a_public_host_once_a_password_is_set(monkeypatch):
    output = _run_serve(monkeypatch, "0.0.0.0", password="a-long-random-string")

    assert "REFUSED" not in output
    # Says what protects it, and never prints the password itself.
    assert "LONGHAND_PASSWORD" in output
    assert "a-long-random-string" not in output


def test_serve_is_quiet_on_loopback(monkeypatch):
    output = _run_serve(monkeypatch, "127.0.0.1")

    assert "REFUSED" not in output
    assert "binding" not in output


# ---------------------------------------------- templates cannot outrun the code
#
# Jinja re-reads a changed template off disk by default; the Python module around
# it does not. A long-running server whose source has moved on therefore renders
# **new templates against old route code**, and what that produces is a 500 on an
# undefined variable — a template asking for a context key the running handler was
# written before it existed.
#
# It happened three times in one afternoon on the same server process (`next`,
# then the folder routes, then `thumbnail`), and every time it read as a bug in
# the new code rather than as a stale process. Frozen templates make an old server
# serve a consistently old page instead, which is obvious and harmless.


def test_templates_do_not_reload_by_default(tmp_path):
    """The property that keeps a running server internally consistent."""
    from videomaker.config import Settings
    from videomaker.web.app import create_app

    app = create_app(Settings(workspace_dir=tmp_path / "workspace"))

    assert app.state.templates.env.auto_reload is False


def test_dev_mode_turns_template_reload_back_on(tmp_path):
    """`--reload` replaces the process on a source change, so hot templates there
    cannot drift away from the code they are rendered by."""
    from videomaker.config import Settings
    from videomaker.web.app import create_app

    app = create_app(Settings(workspace_dir=tmp_path / "workspace"), dev=True)

    assert app.state.templates.env.auto_reload is True


def test_the_reload_entry_point_is_dev(monkeypatch, tmp_path):
    """`create_app_from_env` exists only for `--reload`, so it is dev by definition."""
    from videomaker.web.app import create_app_from_env

    monkeypatch.chdir(tmp_path)
    app = create_app_from_env()

    assert app.state.templates.env.auto_reload is True
