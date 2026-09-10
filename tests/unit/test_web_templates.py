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
from videomaker.web.app import STATIC_DIR, create_app

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


# ------------------------------------------------------------------- the reader
#
# The reader adds the first page-level script in the project. It is vendored for
# exactly the reason htmx is — a local tool must work with no network and must not
# announce a page view to anyone — so the no-CDN guard is extended to cover it and
# every template the reader added.

#: Rendered with a request and a context, unlike `base.html`: these are the
#: reader's own templates and each needs the shape its route hands it.
READER_TEMPLATES = ("read.html", "library.html", "work.html", "_reader_mode.html", "_verse_list.html")


@pytest.fixture
def reader_app(tmp_path):
    return create_app(Settings(workspace_dir=tmp_path / "workspace"), providers="mock")


@pytest.fixture
def reader_client(reader_app):
    return TestClient(reader_app)


def test_readerjs_is_served_as_javascript(reader_client):
    response = reader_client.get("/static/reader.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert "verse-current" in response.text


def test_readerjs_is_small_enough_to_read_in_one_sitting():
    """Under 60 lines of code. Past that, the design is wrong and a library is
    being reinvented — which is the moment to stop, not to minify."""
    lines = (STATIC_DIR / "reader.js").read_text().splitlines()
    code = [line for line in lines if line.strip() and not line.strip().startswith(("//", "/*", "*"))]

    assert len(code) < 60, f"reader.js is {len(code)} lines of code"


def test_the_reading_page_loads_readerjs_from_static(reader_client):
    body = reader_client.get("/read/mock/JHN/1").text
    assert '<script src="/static/reader.js"' in body


def test_only_the_reading_page_pays_for_it(reader_client):
    assert "reader.js" not in reader_client.get("/library").text
    assert "reader.js" not in reader_client.get("/library/mock").text


def test_the_player_is_a_native_audio_element_with_the_generated_vtt(reader_app):
    """No player library, no polyfill: `<audio controls>` and a metadata track."""
    with TestClient(reader_app) as client:
        client.post("/read/mock/JHN/1/audio")
        reader_app.state.jobs.wait_idle(120)
        body = client.get("/read/mock/JHN/1/mode/listen").text
    assert "<audio" in body and "controls" in body
    assert '<track id="reader-cues" kind="metadata"' in body
    assert "reading.vtt" in body


@pytest.mark.parametrize("path", ["/library", "/library/mock", "/read/mock/JHN/1"])
def test_no_reader_page_references_an_external_url(reader_client, path):
    rendered = reader_client.get(path).text
    assert not _ABSOLUTE_URL.search(rendered), f"{path} must not reference an external URL"


@pytest.mark.parametrize("mode", ["source", "brief", "listen"])
def test_no_reader_fragment_references_an_external_url(reader_client, mode):
    rendered = reader_client.get(f"/read/mock/JHN/1/mode/{mode}").text
    assert not _ABSOLUTE_URL.search(rendered)


def test_no_reader_template_carries_an_absolute_url_in_its_source():
    """The rendered checks above can only see the branches a fixture reaches;
    this one reads the files, so a URL in an unexercised `{% if %}` fails too."""
    for name in READER_TEMPLATES:
        source = (STATIC_DIR.parent / "templates" / name).read_text()
        assert not _ABSOLUTE_URL.search(source), f"{name} must not reference an external URL"


def test_readerjs_reaches_nowhere_but_this_server():
    """The timings come off a `<track>` the page already carries; the one request
    it makes is a same-origin POST recording which verse the narration reached,
    and its URL comes out of a hidden field rather than being built here.

    The rule being guarded is the no-CDN one, not "no requests": a local tool must
    work with no network and must not announce a page view to a third party.
    """
    source = (STATIC_DIR / "reader.js").read_text()

    assert not _ABSOLUTE_URL.search(source), "no third party, and nothing off-origin"
    assert "XMLHttpRequest" not in source and "import(" not in source
    assert source.count("fetch(") == 1, "one request, and it is the bookmark"
    assert 'method: "POST"' in source
    assert "reader-place" in source, "the URL is read from the page, not written here"
