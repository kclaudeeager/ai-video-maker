"""One app, two doors: what a reading server mounts, and what it cannot.

`Audience.READER` is a **deployment** choice rather than a role. A reading server
does not register the studio routers at all, so `/projects/...` is a 404 because
there is no route — not because a check said no. That is the whole reason this
needs no accounts, no roles and no permission system, and
`test_no_studio_route_survives_reader_mode` is the assertion that it stays true
as routes are added.
"""

import pytest
from fastapi.testclient import TestClient

from videomaker.config import Audience, Settings, load_settings
from videomaker.web.app import create_app

#: Every path that can produce something. A route added to a studio router lands
#: under one of these prefixes, which is what makes the sweep below meaningful
#: rather than a list somebody has to remember to extend.
STUDIO_PREFIXES = ("/projects", "/start", "/guide")


def app_for(audience: Audience, tmp_path):
    return create_app(
        Settings(workspace_dir=tmp_path / "workspace", audience=audience), providers="mock"
    )


@pytest.fixture
def studio(tmp_path):
    return TestClient(app_for(Audience.STUDIO, tmp_path))


@pytest.fixture
def reader(tmp_path):
    return TestClient(app_for(Audience.READER, tmp_path))


# ------------------------------------------------------------------ the default


def test_a_server_is_a_studio_unless_it_is_told_otherwise():
    assert Settings().audience is Audience.STUDIO


def test_the_audience_can_be_set_in_config(tmp_path):
    (tmp_path / "config.yaml").write_text("audience: reader\n")
    assert load_settings(tmp_path / "config.yaml").audience is Audience.READER


def test_an_audience_that_is_not_one_is_ignored_rather_than_fatal(tmp_path):
    """A typo in a config file must not stop the tool starting."""
    (tmp_path / "config.yaml").write_text("audience: superuser\n")
    assert load_settings(tmp_path / "config.yaml").audience is Audience.STUDIO


# --------------------------------------------------------------- what is mounted


@pytest.mark.parametrize(
    "path",
    ["/", "/start", "/start/video", "/start/read", "/start/document", "/guide"],
)
def test_the_studio_serves_its_own_routes(studio, path):
    assert studio.get(path).status_code == 200


@pytest.mark.parametrize(
    "path",
    ["/start", "/start/video", "/start/read", "/start/document", "/guide", "/projects/anything"],
)
def test_a_reading_server_has_no_studio_route(reader, path):
    assert reader.get(path).status_code == 404


def test_no_studio_route_survives_reader_mode(tmp_path):
    """Swept from the route table rather than from a list, so a studio route
    added later is covered the day it is added."""
    reading = app_for(Audience.READER, tmp_path)

    mounted = [getattr(route, "path", "") for route in reading.routes]
    offenders = [
        path for path in mounted if path.startswith(STUDIO_PREFIXES) and path != "/"
    ]

    assert offenders == [], f"a reading server mounted {offenders}"


def test_a_reading_server_cannot_be_posted_to_either(reader):
    """A 404 on GET and a 405 on POST would still be a route. Neither exists."""
    assert reader.post("/projects", data={"topic": "x"}).status_code == 404
    assert reader.post("/start/document", files={"document": ("a.md", b"# x")}).status_code == 404


# ------------------------------------------------------------- the front door


def test_the_library_is_the_front_door_of_a_reading_server(reader):
    landing = reader.get("/", follow_redirects=False)

    assert landing.status_code == 307
    assert landing.headers["location"] == "/library"
    assert reader.get("/").status_code == 200


def test_the_workspace_is_the_front_door_of_a_studio(studio):
    assert studio.get("/", follow_redirects=False).status_code == 200


# ------------------------------------------------------------------ what reads


@pytest.mark.parametrize("path", ["/library", "/library/mock", "/read/mock/JHN/1"])
def test_reading_works_on_both_doors(studio, reader, path):
    assert studio.get(path).status_code == 200
    assert reader.get(path).status_code == 200


def test_the_reading_media_route_is_mounted_for_a_reader(reader):
    """404 because the file is not there, not because the route is missing."""
    response = reader.get("/media/reading/mock/deadbeef/reading.mp3")

    assert response.status_code == 404
    assert response.json()["detail"] == "not found", "the reader's own 404, not the router's"


# -------------------------------------------------------------------- the nav


def test_the_masthead_offers_no_studio_link_to_a_reader(reader):
    body = reader.get("/library").text

    assert ">Library<" in body
    for absent in (">Workspace<", ">Start<", ">How it works<"):
        assert absent not in body, f"a reading server linked to {absent}"


def test_the_masthead_still_offers_them_in_a_studio(studio):
    body = studio.get("/library").text

    for present in (">Workspace<", ">Start<", ">How it works<"):
        assert present in body


def test_the_bare_shell_still_renders_without_an_audience(tmp_path):
    """`base.html` is rendered with no request and no context by the template
    test; `_nav.html` must not need the global to exist."""
    app = app_for(Audience.STUDIO, tmp_path)
    env = app.state.templates.env
    env.globals.pop("audience", None)

    assert "Longhand" in env.get_template("base.html").render()
