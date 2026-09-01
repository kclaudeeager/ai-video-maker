"""The single-password gate, and the startup refusal that makes it unskippable.

Two separate guarantees, and the second is the one that actually protects anybody:

1. **With a password set, everything is behind it** — routes, `/static`,
   `/assets`, `/media`. A gate with a hole in it is not a gate, and the classic
   hole is a FastAPI dependency that covers the routers but not the mounted
   static apps.
2. **A public bind with no password does not start.** The previous behaviour was
   three red warnings followed by serving anyway, which is the exact shape of an
   accidentally-public deployment: the warning scrolls past, the open port stays.

`/healthz` is the one deliberate exception, because a platform health check
cannot carry credentials.
"""

import base64

import pytest
from fastapi.testclient import TestClient

from videomaker.config import Settings
from videomaker.web.app import create_app
from videomaker.web.auth import (
    PASSWORD_ENV,
    USERNAME,
    PasswordRequired,
    is_loopback,
    require_password_for,
)
from videomaker.web.voices import clear_voice_cache

PASSWORD = "a-long-random-string"


def _basic(user: str = USERNAME, password: str = PASSWORD) -> dict[str, str]:
    raw = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(workspace_dir=tmp_path / "workspace")


@pytest.fixture
def guarded(settings, monkeypatch) -> TestClient:
    monkeypatch.setenv(PASSWORD_ENV, PASSWORD)
    clear_voice_cache()
    return TestClient(create_app(settings))


@pytest.fixture
def open_app(settings, monkeypatch) -> TestClient:
    monkeypatch.delenv(PASSWORD_ENV, raising=False)
    clear_voice_cache()
    return TestClient(create_app(settings))


# ------------------------------------------------------------- the gate itself


@pytest.mark.parametrize("path", ["/", "/guide", "/static/style.css"])
def test_every_path_is_refused_without_credentials(guarded, path):
    response = guarded.get(path)

    assert response.status_code == 401
    # Without this header a browser never offers a prompt, so the gate would
    # simply look like a broken site.
    assert response.headers["WWW-Authenticate"].startswith("Basic realm=")


def test_the_static_mount_is_behind_the_gate_too(guarded):
    """The hole a router-level dependency leaves. `/static` and `/assets` are
    separate ASGI apps, so the gate has to sit above them, not inside the routers."""
    assert guarded.get("/static/style.css").status_code == 401
    assert guarded.get("/assets/fonts/SpaceGrotesk-Regular.ttf").status_code == 401


def test_the_media_route_is_behind_the_gate(guarded):
    """The one that serves arbitrary files out of a project folder."""
    assert guarded.get("/media/anything/output/final_wide.mp4").status_code == 401


def test_the_right_password_is_let_through(guarded):
    response = guarded.get("/", headers=_basic())

    assert response.status_code == 200
    assert "Longhand" in response.text


def test_a_wrong_password_is_refused(guarded):
    assert guarded.get("/", headers=_basic(password="nearly")).status_code == 401


def test_a_wrong_username_is_refused(guarded):
    assert guarded.get("/", headers=_basic(user="admin")).status_code == 401


@pytest.mark.parametrize(
    "header",
    [
        "",
        "Basic",
        "Basic ",
        "Bearer " + base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode(),
        "Basic !!!not-base64!!!",
        # Valid base64, but no colon: `partition` must not read that as a match
        # with an empty password.
        "Basic " + base64.b64encode(b"longhand").decode(),
    ],
)
def test_a_malformed_authorization_header_is_refused_not_crashed(guarded, header):
    response = guarded.get("/", headers={"Authorization": header})

    assert response.status_code == 401


def test_an_empty_password_never_matches_an_empty_credential(settings, monkeypatch):
    """The gate is only installed when a password is set, so this asserts the
    reason: an empty password with an empty credential must not be a match."""
    monkeypatch.setenv(PASSWORD_ENV, "")
    clear_voice_cache()
    client = TestClient(create_app(settings))

    # No gate at all, rather than a gate that accepts an empty string.
    assert client.get("/").status_code == 200


def test_healthz_answers_without_credentials(guarded):
    """A platform health check cannot carry credentials, so this one path is
    public — and it reads nothing from the workspace."""
    response = guarded.get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_the_password_is_never_echoed_back(guarded):
    """A 401 body that quoted the expected password would be a fine way to leak it."""
    body = guarded.get("/").text

    assert PASSWORD not in body


def test_loopback_use_needs_no_password(open_app):
    """The local single-user case the whole product is built around. A prompt on
    127.0.0.1 would be friction protecting nothing."""
    assert open_app.get("/").status_code == 200


# ------------------------------------------------------ the startup refusal


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.0.0.5"])
def test_loopback_binds_are_allowed_with_no_password(host):
    assert is_loopback(host)
    require_password_for(host, "")  # does not raise


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "10.0.0.4"])
def test_a_public_bind_with_no_password_is_refused(host):
    assert not is_loopback(host)

    with pytest.raises(PasswordRequired) as refused:
        require_password_for(host, "")

    # The refusal has to say what to do about it, or it is just an obstacle.
    assert PASSWORD_ENV in str(refused.value)
    assert "127.0.0.1" in str(refused.value)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10"])
def test_a_public_bind_with_a_password_is_allowed(host):
    require_password_for(host, PASSWORD)  # does not raise


def test_an_unresolvable_host_is_treated_as_public():
    """`is_loopback` cannot parse a DNS name, and the safe reading of "I do not
    know what this is" is "assume it is exposed"."""
    assert not is_loopback("longhand.example.com")

    with pytest.raises(PasswordRequired):
        require_password_for("longhand.example.com", "")
