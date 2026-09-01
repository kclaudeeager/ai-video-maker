"""The upload card on the render page: shown at its real size, and downloadable.

The `thumbnail` stage has written `output/thumbnail.jpg` since M3 Task 15, and until
now nothing in the UI mentioned it — while the guidance panel was already telling
people to "download them". The picture existed and was unreachable.

Two things are asserted here that a "does the page mention it" check would miss:

* **Freshness is `stage_is_current`, not mtime.** A stale card is the one artefact
  you would upload without noticing, because it still looks finished — so the page
  has to say so rather than quietly serve last week's headline.
* **The download lands under a distinguishable name.** Every project writes
  `thumbnail.jpg`; three downloads without the id prefix give `thumbnail.jpg`,
  `thumbnail(1).jpg` and `thumbnail(2).jpg`, which is a folder of pictures you
  cannot match to their videos.

The block only renders once there is a finished cut, so these fabricate an output
file rather than run a real encode — the encode itself is covered in
`test_web_render_gate.py`, at the cost of ten minutes a run.
"""

import pytest
from fastapi.testclient import TestClient

from videomaker.config import Settings
from videomaker.models import Aspect
from videomaker.pipeline.render import output_relpath
from videomaker.pipeline.thumbnail import STAGE as THUMBNAIL_STAGE
from videomaker.pipeline.thumbnail import thumbnail_relpath
from videomaker.project import ProjectStore
from videomaker.runner import stage_cache_for
from videomaker.web.app import create_app
from videomaker.web.routes.render import thumbnail_view
from videomaker.web.voices import clear_voice_cache


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(workspace_dir=tmp_path / "workspace")


@pytest.fixture
def app(settings):
    clear_voice_cache()
    return create_app(settings)


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture
def store(app) -> ProjectStore:
    return app.state.store


# A one-pixel JPEG, so `/media` really serves image bytes rather than a text file
# that happens to end in `.jpg`.
_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300ff"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffff"
    "ffffffffffffffffffffffffffc2000b080001000101011100ff"
    "c40014000100000000000000000000000000000009ffda0008010100013f10"
)


def _project(store: ProjectStore, *, rendered=True, thumbnail=True):
    """A project with a finished wide cut, and optionally a drawn card."""
    project = store.create(topic="how ssds work", template="tech_explainer")
    root = store.path_for(project.id)
    if rendered:
        path = root / output_relpath(Aspect.WIDE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not really an mp4, but it exists")
    if thumbnail:
        path = root / thumbnail_relpath()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_JPEG)
    return project


def _mark_current(store: ProjectStore, project):
    """Record the thumbnail stage as run, so the view calls the card fresh.

    Written through `STAGE_UNITS` and `status_key` rather than a literal key, so
    this helper cannot claim a stage is current in a shape `stage_is_current` does
    not actually read — which would make every assertion below pass vacuously.
    """
    from videomaker.runner import STAGE_UNITS, status_key

    # `produced` reads `Project.thumbnail_path`, not the file on disk — the stage
    # records what it drew on the model — so writing only the bytes leaves the unit
    # unproduced and the stage permanently stale.
    project.thumbnail_path = thumbnail_relpath()
    store.save(project)

    cache = stage_cache_for(store, project.id)
    for unit in STAGE_UNITS[THUMBNAIL_STAGE](project):
        cache.mark(status_key(THUMBNAIL_STAGE, unit.unit), unit.fingerprint)
    cache.save()


# ------------------------------------------------------------------- the view


def test_a_project_with_no_card_reports_missing(store):
    project = _project(store, thumbnail=False)

    view = thumbnail_view(store, project)

    assert view.exists is False
    assert view.state == "missing"


def test_a_card_whose_stage_never_ran_reports_stale(store):
    """The file is there and nothing recorded drawing it, so the page must not
    call it current — this is the state a project rendered before M3 lands in."""
    project = _project(store)

    view = thumbnail_view(store, project)

    assert view.exists is True
    assert view.state == "stale"


def test_a_card_recorded_as_drawn_reports_ready(store):
    project = _project(store)
    _mark_current(store, project)

    assert thumbnail_view(store, project).state == "ready"


def test_the_card_is_served_from_the_project_relative_media_url(store):
    project = _project(store)

    view = thumbnail_view(store, project)

    assert view.url == f"/media/{project.id}/{thumbnail_relpath()}"
    assert view.relpath == thumbnail_relpath()


def test_the_download_name_carries_the_project_id(store):
    """Every project writes `thumbnail.jpg`, so the bare name is unusable in a
    Downloads folder holding more than one."""
    project = _project(store)

    assert thumbnail_view(store, project).download_name == f"{project.id}-thumbnail.jpg"


# ------------------------------------------------------------------- the page


def test_the_render_page_shows_the_card_and_offers_it_for_download(client, store):
    project = _project(store)
    _mark_current(store, project)

    body = client.get(f"/projects/{project.id}/render").text

    url = f"/media/{project.id}/{thumbnail_relpath()}"
    assert f'<img class="thumbnail-card" src="{url}"' in body
    assert f'download="{project.id}-thumbnail.jpg"' in body
    # Drawn at the real aspect, so what is on screen is what gets uploaded.
    assert 'width="1280" height="720"' in body


def test_the_page_serves_the_real_bytes(client, store):
    project = _project(store)

    served = client.get(f"/media/{project.id}/{thumbnail_relpath()}")

    assert served.status_code == 200
    assert served.content == _JPEG
    assert served.headers["content-type"] == "image/jpeg"


def test_a_stale_card_says_so_rather_than_looking_finished(client, store):
    project = _project(store)

    body = client.get(f"/projects/{project.id}/render").text

    assert "This card is from an earlier run" in body


def test_a_fresh_card_carries_no_warning(client, store):
    project = _project(store)
    _mark_current(store, project)

    body = client.get(f"/projects/{project.id}/render").text

    assert "This card is from an earlier run" not in body


def test_a_project_with_no_card_is_told_it_is_simply_not_drawn_yet(client, store):
    """Not an error: the stage is last in the pipeline and arrived in M3, so a cut
    rendered before it has a video and no card, and nothing is wrong."""
    project = _project(store, thumbnail=False)

    body = client.get(f"/projects/{project.id}/render").text

    assert "No thumbnail has been drawn yet" in body
    assert "<img class=\"thumbnail-card\"" not in body


def test_the_card_is_not_offered_before_there_is_anything_to_upload(client, store):
    """The block sits inside the finished-cut branch: a thumbnail with no video is
    not a delivery, and offering it would imply the render was done."""
    project = _project(store, rendered=False)

    body = client.get(f"/projects/{project.id}/render").text

    assert "thumbnail-card" not in body
