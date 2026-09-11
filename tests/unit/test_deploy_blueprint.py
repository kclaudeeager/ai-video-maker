"""What the Render blueprint has to declare for the deployed app to be correct.

These are settings whose absence is invisible locally and expensive in
production: the app runs, serves pages, and quietly builds wrong URLs. A unit
test is the only place the mistake is cheap to catch.
"""

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
BLUEPRINT = REPO / "render.yaml"
DOCKERFILE = REPO / "Dockerfile"


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


def test_the_workspace_lives_on_the_disk_when_there_is_one():
    """`WORKSPACE_DIR` outside the mount means every project is lost on restart.

    Conditional because the free plan has no disk at all — there the workspace is
    ephemeral by definition and there is nothing to be inside of. The moment a
    disk is declared, though, the workspace has to be on it, or the disk is paid
    for and unused.
    """
    service = _web_service()
    disk = service.get("disk")
    if disk is None:
        assert service["plan"] == "free", "only the free plan may go without a disk"
        return

    assert str(_env(service)["WORKSPACE_DIR"]["value"]).startswith(f'{disk["mountPath"]}/')


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


def test_the_ml_extra_is_a_build_argument():
    """`ML=0` is the free-tier build: no `ml` extra, no model stage, 980 MB
    against 2.56 GB. It only works if the Dockerfile keeps asking — a sync that
    goes back to a hard-coded `--extra ml` would build the large image silently
    and only run out of memory on the box that cannot afford it.
    """
    dockerfile = DOCKERFILE.read_text()

    lines = dockerfile.splitlines()

    assert "ARG ML=1" in lines, "ML must default to the full build"
    # Global, above the first stage, or `FROM models-${ML}` cannot read it.
    first_stage = next(i for i, line in enumerate(lines) if line.startswith("FROM "))
    assert lines.index("ARG ML=1") < first_stage, "ARG ML must precede the first FROM"
    assert "FROM models-${ML} AS models" in dockerfile
    for stage in ("models-0", "models-1"):
        assert f"AS {stage}" in dockerfile, f"{stage} is what `FROM models-${{ML}}` selects"
    assert "uv sync --frozen --extra ml;" in dockerfile
    assert "else uv sync --frozen; fi" in dockerfile


def test_the_build_matches_what_the_plan_can_run():
    """`ML` and `plan` are one decision written in two places, and a mismatch is
    silent both ways.

    `ML=1` on free builds a 2.56 GB image whose models cannot be loaded in 512 MB
    — the deploy succeeds and narration OOMs. `ML=0` on a paid plan quietly
    removes narration from a box that was bought to do it, and the only symptom is
    a Listen tab that never appears.
    """
    service = _web_service()

    expected = "0" if service["plan"] == "free" else "1"
    assert _env(service)["ML"]["value"] == expected


def test_a_reading_server_is_given_something_to_read():
    """A reading server mounts no import route and a free container has no shell,
    so a blueprint without this deploys a library nothing can ever fill."""
    env = _env(_web_service())
    if env.get("AUDIENCE", {}).get("value") != "reader":
        return

    from videomaker.corpus.catalogue import CATALOGUE

    wanted = [w.strip() for w in str(env["IMPORT_WORKS"]["value"]).split(",") if w.strip()]
    assert wanted, "a reading server with nothing to import has an empty shelf forever"
    for work_id in wanted:
        assert work_id in CATALOGUE, f"{work_id} is not a catalogue id"
