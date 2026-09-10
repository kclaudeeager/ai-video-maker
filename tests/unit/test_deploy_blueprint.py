"""What the Render blueprint has to declare for the deployed app to be correct.

These are settings whose absence is invisible locally and expensive in
production: the app runs, serves pages, and quietly builds wrong URLs. A unit
test is the only place the mistake is cheap to catch.
"""

from pathlib import Path

import yaml

BLUEPRINT = Path(__file__).resolve().parents[2] / "render.yaml"


def _web_service() -> dict:
    services = yaml.safe_load(BLUEPRINT.read_text())["services"]
    return next(service for service in services if service["type"] == "web")


def _env(service: dict) -> dict[str, object]:
    return {entry["key"]: entry for entry in service["envVars"]}


def test_the_proxy_is_trusted_so_urls_come_out_https():
    """Uvicorn ignores `X-Forwarded-Proto` from a non-loopback peer by default.

    Render's proxy is not loopback, so without this the feed address and every
    enclosure URL in it are `http://` on an `https://` site — measured, not
    assumed: the same request rendered `http://longhand.onrender.com/...` with
    the default and `https://...` with this set.
    """
    assert _env(_web_service())["FORWARDED_ALLOW_IPS"]["value"] == "*"


def test_the_workspace_lives_on_the_disk():
    """`WORKSPACE_DIR` outside the mount means every project is lost on restart."""
    service = _web_service()
    mount = service["disk"]["mountPath"]

    assert str(_env(service)["WORKSPACE_DIR"]["value"]).startswith(f"{mount}/")


def test_the_password_is_generated_rather_than_committed():
    """`serve` refuses a public bind without one, and the repo must never hold it."""
    entry = _env(_web_service())["LONGHAND_PASSWORD"]

    assert entry.get("generateValue") is True
    assert "value" not in entry


def test_provider_keys_are_prompted_for_not_read_from_the_repo():
    env = _env(_web_service())
    for key in ("GROQ_API_KEY", "GEMINI_API_KEY", "PEXELS_API_KEY", "CLOUDFLARE_API_TOKEN"):
        assert env[key]["sync"] is False, f"{key} must be sync: false"
        assert "value" not in env[key]

