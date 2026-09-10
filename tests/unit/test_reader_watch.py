"""A reader may watch what exists, and commission nothing.

Two different things share the word *watch*, and the first pass at the reading
view removed both when it meant to remove one:

* **commissioning** a video materialises a `Project`, which needs the three gates
  and a studio to approve them in — correctly absent for a reader;
* **playing one that already exists** needs neither, and is the whole consumer
  ask.

So a reader is offered the tab only when there is something behind it, and never
a control that would start work.
"""

from fastapi.testclient import TestClient

from videomaker.config import Audience, Settings
from videomaker.corpus.materialise import materialise
from videomaker.corpus.models import UnitRef
from videomaker.models import Aspect, OutputSpec
from videomaker.web.app import create_app
from videomaker.web.routes.library import ReadMode, modes_for, rendered_for

REF = UnitRef(work_id="mock", book="JHN", chapter=1)
RENDERED = "output/final_wide.mp4"


def app_for(audience: Audience, tmp_path):
    return create_app(
        Settings(workspace_dir=tmp_path / "workspace", audience=audience), providers="mock"
    )


def render_one(app, *, relpath: str = RENDERED, write: bool = True):
    """A project materialised from the passage, with a finished wide cut."""
    store = app.state.store
    from videomaker.corpus.audio import reader_deps

    unit = reader_deps(app.state.settings).provider("corpus").unit(REF)
    project = materialise(REF, unit=unit, store=store)
    project.outputs[Aspect.WIDE] = OutputSpec(
        aspect=Aspect.WIDE, width=1920, height=1080, scene_ids=["s01"], video_path=relpath
    )
    store.save(project)
    if write:
        target = store.path_for(project.id) / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x00\x00\x00\x18ftypmp42fake-video-bytes")
    return project


# -------------------------------------------------------------- what is offered


def test_a_reader_with_nothing_rendered_has_no_watch_tab(tmp_path):
    app = app_for(Audience.READER, tmp_path)

    with TestClient(app) as client:
        assert ">Watch<" not in client.get("/read/mock/JHN/1").text
        assert client.get("/read/mock/JHN/1/mode/watch").status_code == 404


def test_a_reader_with_a_rendered_video_can_watch_it(tmp_path):
    app = app_for(Audience.READER, tmp_path)
    render_one(app)

    with TestClient(app) as client:
        page = client.get("/read/mock/JHN/1").text
        panel = client.get("/read/mock/JHN/1/mode/watch")

    assert ">Watch<" in page
    assert panel.status_code == 200
    assert "<video" in panel.text
    assert "/media/watch/mock/JHN/1" in panel.text


def test_a_reader_is_never_offered_a_way_to_commission_one(tmp_path):
    app = app_for(Audience.READER, tmp_path)
    render_one(app)

    with TestClient(app) as client:
        panel = client.get("/read/mock/JHN/1/mode/watch").text
        started = client.post("/read/mock/JHN/1/watch")

    assert "Start a video of this passage" not in panel
    assert "<form" not in panel
    assert "/projects/" not in panel, "and no link into a studio that is not there"
    assert started.status_code == 404


def test_the_studio_still_commissions(tmp_path):
    app = app_for(Audience.STUDIO, tmp_path)

    with TestClient(app) as client:
        panel = client.get("/read/mock/JHN/1/mode/watch").text

    assert "Start a video of this passage" in panel
    assert ">Watch<" in client.get("/read/mock/JHN/1").text


def test_the_shelf_offers_no_watch_because_it_has_no_chapter(tmp_path):
    """`modes_for` with no `ref` cannot know whether anything is rendered."""
    app = app_for(Audience.READER, tmp_path)
    render_one(app)
    (work,) = _works(app)

    assert ReadMode.WATCH not in modes_for(work, app.state.settings)
    assert ReadMode.WATCH in modes_for(
        work, app.state.settings, ref=REF, store=app.state.store
    )


def _works(app):
    from videomaker.corpus.audio import reader_deps

    return reader_deps(app.state.settings).provider("corpus").works()


def test_a_project_with_no_render_yet_is_not_something_to_watch(tmp_path):
    app = app_for(Audience.READER, tmp_path)
    store = app.state.store
    from videomaker.corpus.audio import reader_deps

    unit = reader_deps(app.state.settings).provider("corpus").unit(REF)
    materialise(REF, unit=unit, store=store)  # started, never rendered

    assert rendered_for(store, REF) is None
    with TestClient(app) as client:
        assert ">Watch<" not in client.get("/read/mock/JHN/1").text


# ------------------------------------------------------------------ the route


def test_the_narrow_route_serves_the_render(tmp_path):
    app = app_for(Audience.READER, tmp_path)
    render_one(app)

    with TestClient(app) as client:
        response = client.get("/media/watch/mock/JHN/1")

    assert response.status_code == 200
    assert response.headers["content-type"] == "video/mp4"
    assert response.content.startswith(b"\x00\x00\x00\x18ftyp")


def test_the_narrow_route_serves_only_the_render(tmp_path):
    """It resolves one artefact from a passage. There is no path to supply."""
    app = app_for(Audience.READER, tmp_path)
    project = render_one(app)
    secret = app.state.store.path_for(project.id) / "project.json"
    assert secret.is_file()

    with TestClient(app) as client:
        for probe in ("/media/watch/mock/JHN/1/project.json", "/media/watch/mock/JHN/1/../.."):
            assert client.get(probe).status_code == 404


def test_a_reading_server_does_not_mount_project_media_at_all(tmp_path):
    """`/media/{project}/{path}` would serve every script and take in the
    workspace. On a reading server the route does not exist."""
    app = app_for(Audience.READER, tmp_path)
    project = render_one(app)

    with TestClient(app) as client:
        assert client.get(f"/media/{project.id}/{RENDERED}").status_code == 404
        assert client.get(f"/media/{project.id}/project.json").status_code == 404


def test_the_studio_still_has_project_media(tmp_path):
    app = app_for(Audience.STUDIO, tmp_path)
    project = render_one(app)

    with TestClient(app) as client:
        assert client.get(f"/media/{project.id}/{RENDERED}").status_code == 200


def test_a_render_recorded_but_missing_from_disk_is_a_404(tmp_path):
    app = app_for(Audience.READER, tmp_path)
    render_one(app, write=False)

    with TestClient(app) as client:
        assert client.get("/media/watch/mock/JHN/1").status_code == 404


def test_a_traversing_video_path_on_the_project_is_refused(tmp_path):
    """`video_path` comes off `project.json`, which a user may hand-edit.

    The escape target is a file that really exists, so a pass here is a refusal
    rather than a miss: `workspace/projects/<id>/../../../` is `tmp_path`.
    """
    outside = tmp_path / "secret.txt"
    outside.write_text("not yours")
    app = app_for(Audience.READER, tmp_path)
    render_one(app, relpath="../../../secret.txt", write=False)

    with TestClient(app) as client:
        response = client.get("/media/watch/mock/JHN/1")

    assert response.status_code == 404
    assert outside.is_file(), "the target must exist for this to prove anything"


def test_an_unknown_passage_is_a_404(tmp_path):
    app = app_for(Audience.READER, tmp_path)

    with TestClient(app) as client:
        assert client.get("/media/watch/mock/JHN/99").status_code == 404
        assert client.get("/media/watch/nope/JHN/1").status_code == 404
        assert client.get("/media/watch/mock/ZZZ/1").status_code == 404
