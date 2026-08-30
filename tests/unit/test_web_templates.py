"""The document shell: static assets ship and load, and nothing comes from a CDN.

The no-CDN rule (M2 global constraints) is the reason these tests exist. It is a
rule that decays silently — a template gains one `<script src="https://...">`,
everything still works on the author's machine, and the tool has quietly become
online-only and started announcing itself to a third party on every page load.
So the regression guard is textual and blunt: the rendered shell may not contain
an absolute URL at all.
"""

import re

import pytest
from fastapi.testclient import TestClient

from videomaker.config import Settings
from videomaker.web.app import create_app

#: Any absolute URL in markup. Deliberately not narrowed to `src=`/`href=`:
#: an `@import` in an inline style or a fetch in an inline script would evade
#: an attribute-shaped pattern while breaking the constraint just as thoroughly.
_ABSOLUTE_URL = re.compile(r"https?://", re.IGNORECASE)


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(workspace_dir=tmp_path))


@pytest.fixture
def client(app):
    return TestClient(app)


def _render_base(app) -> str:
    """Render `base.html` on its own, with no request and no child template."""
    return app.state.templates.get_template("base.html").render()


def test_templates_are_on_app_state(app):
    from fastapi.templating import Jinja2Templates

    assert isinstance(app.state.templates, Jinja2Templates)


def test_vendored_htmx_is_served_as_javascript(client):
    response = client.get("/static/vendor/htmx.min.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert "htmx" in response.text


def test_stylesheet_is_served(client):
    response = client.get("/static/style.css")
    assert response.status_code == 200
    assert "css" in response.headers["content-type"]


def test_static_mount_does_not_escape_its_directory(client):
    assert client.get("/static/../app.py").status_code == 404


def test_base_loads_htmx_from_the_vendored_copy(app):
    assert "/static/vendor/htmx.min.js" in _render_base(app)


def test_base_has_no_external_asset_reference(app):
    """The no-CDN guard. Also covers `_nav.html`, which base includes."""
    rendered = _render_base(app)
    assert not _ABSOLUTE_URL.search(rendered), "base.html must not reference an external URL"


def test_base_defines_the_documented_blocks(app):
    """A child template overriding these must actually change the output."""
    env = app.state.templates.env
    child = env.from_string(
        "{% extends 'base.html' %}"
        "{% block title %}TTT{% endblock %}"
        "{% block content %}CCC{% endblock %}"
        "{% block scripts %}SSS{% endblock %}"
    )
    rendered = child.render()
    assert "TTT" in rendered
    assert "CCC" in rendered
    assert "SSS" in rendered
