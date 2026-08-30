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


# `serve` itself is not in the plan's test block; these two guard the one thing
# the task calls out as security-relevant — the non-loopback warning.


def _run_serve(monkeypatch, host: str):
    import uvicorn
    from typer.testing import CliRunner

    from videomaker.cli import app as cli_app

    calls: list[dict] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append(kw))
    result = CliRunner().invoke(cli_app, ["serve", "--host", host, "--port", "8123"])
    assert result.exit_code == 0, result.output
    assert calls == [{"host": host, "port": 8123}]
    # rich hard-wraps at the terminal width; collapse it so asserts see one line.
    return " ".join(result.output.split())


def test_serve_warns_loudly_on_a_non_loopback_host(monkeypatch):
    output = _run_serve(monkeypatch, "0.0.0.0")
    assert "WARNING" in output
    assert "NOT a loopback address" in output
    assert "NO authentication" in output
    assert "read and write any path" in output


def test_serve_is_quiet_on_loopback(monkeypatch):
    assert "WARNING" not in _run_serve(monkeypatch, "127.0.0.1")
