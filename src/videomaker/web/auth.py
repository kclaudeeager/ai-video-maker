"""The single-password gate that makes a public deployment safe.

M6, per `docs/multi-tenant-design.md`: *"where the single-password gate belongs so
you can deploy safely."* This is deliberately **not** multi-tenant auth. There are
no accounts, no sessions and no user table, because there is exactly one owner and
inventing a user model now would be the wrong thing to have to migrate later
(M7+ is where accounts belong, if they ever do).

**Why the app cannot simply default to open.** Everything behind this gate is
dangerous in a way that is easy to underestimate:

* `POST /projects` spends the owner's Groq, Gemini, Cloudflare and Pexels quota.
* `/media/{id}/{path}` serves any file inside a project folder.
* the render gate starts multi-minute encodes on the host's CPU.

So the rule is **fail closed, loudly**: binding a non-loopback address with no
password set is refused at startup by `require_password_for`, rather than served
open with a warning nobody reads. Loopback stays open with no password because
that is the local-first single-user case the whole product is built around, and a
password prompt on `127.0.0.1` would be friction protecting nothing.

HTTP Basic rather than a login form and a session cookie: it is stateless, it
needs no session store or CSRF token, it works from `curl` and from a browser, and
it survives the app restarting. Its real weakness — no clean logout — is not worth
a session table for a deployment with one user.

The credential comparison is `hmac.compare_digest`, so a wrong password takes the
same time to reject regardless of how many leading characters were right.
"""

import base64
import binascii
import hmac
import ipaddress
import secrets

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

#: Env var holding the shared password. Read at app construction, never logged.
PASSWORD_ENV = "LONGHAND_PASSWORD"

#: The username half of the Basic pair. Fixed: there is one account, and asking a
#: person to remember a username as well as a password protects nothing.
USERNAME = "longhand"

#: Paths served before the gate. `/healthz` only, and only because a platform
#: health check cannot carry credentials — Render, Docker and compose all probe an
#: unauthenticated URL. It returns a status and a version string and reads nothing
#: from the workspace, so it discloses nothing a port scan would not.
PUBLIC_PATHS: frozenset[str] = frozenset({"/healthz"})

REALM = "Longhand"


class PasswordRequired(RuntimeError):
    """Raised at startup when a public bind has no password set."""


def is_loopback(host: str) -> bool:
    """True for `localhost` and any address in a loopback range.

    Mirrors `cli._is_loopback` exactly. The two are separate because the CLI
    decides whether to *warn* and this decides whether to *refuse*, and a shared
    helper that did both would make one of those two behaviours accidental.
    """
    if host in {"localhost", "localhost."}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def require_password_for(host: str, password: str) -> None:
    """Refuse to serve a non-loopback bind with no password.

    Called before the server starts, so the failure is a startup error the
    operator sees rather than an open service they do not.
    """
    if is_loopback(host) or password:
        return
    raise PasswordRequired(
        f"refusing to bind {host} with no password: everything behind this UI spends "
        f"your provider quota, reads your project files and starts encodes on this "
        f"machine. Set {PASSWORD_ENV} to a long random string, or bind 127.0.0.1."
    )


def _credentials_match(header: str, password: str) -> bool:
    """Constant-time check of one `Authorization: Basic ...` header."""
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return False
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False
    user, sep, given = decoded.partition(":")
    if not sep:
        return False
    # Both halves are compared, and both in constant time: `&` rather than `and`
    # so a wrong username does not short-circuit and leak, by timing, that the
    # password was never even looked at.
    return bool(
        hmac.compare_digest(user, USERNAME) & hmac.compare_digest(given, password)
    )


class PasswordGate:
    """ASGI middleware demanding HTTP Basic on everything but `PUBLIC_PATHS`.

    Written at the ASGI layer rather than as a FastAPI dependency so it covers the
    mounted `StaticFiles` apps too. A dependency added to the routers would leave
    `/static` and `/assets` open — harmless in themselves, but the point of a gate
    is that there is nothing to reason about.
    """

    def __init__(self, app: ASGIApp, password: str) -> None:
        self.app = app
        self.password = password

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return
        header = Headers(scope=scope).get("authorization", "")
        if _credentials_match(header, self.password):
            await self.app(scope, receive, send)
            return
        response = PlainTextResponse(
            "Longhand needs a password.",
            status_code=401,
            headers={"WWW-Authenticate": f'Basic realm="{REALM}", charset="UTF-8"'},
        )
        await response(scope, receive, send)


def generated_password() -> str:
    """A password worth using, for the deploy docs to print."""
    return secrets.token_urlsafe(24)
