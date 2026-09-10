"""Watch, from the page you are reading: the reader hands you to gate 1.

The point of the test is the handover. A reader asking to watch a chapter is a
creator starting a project, so what they get is an ordinary project on the
ordinary path — **the three gates are not bypassed and no fourth is added**.
"""

import pytest
from fastapi.testclient import TestClient

from videomaker.config import Settings
from videomaker.models import Status
from videomaker.runner import derive_status, stage_cache_for
from videomaker.web.app import create_app


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(workspace_dir=tmp_path / "workspace"), providers="mock")


def test_watching_a_passage_lands_at_gate_one_with_a_script_to_read(app):
    with TestClient(app) as client:
        panel = client.get("/read/mock/JHN/1/mode/watch")
        assert panel.status_code == 200
        assert "Start a video of this passage" in panel.text
        assert app.state.store.list_ids() == [], "looking at the panel creates nothing"

        started = client.post("/read/mock/JHN/1/watch", follow_redirects=False)
        assert started.status_code == 303
        project_id = started.headers["location"].removeprefix("/projects/")
        assert app.state.jobs.wait_idle(120)

        page = client.get(f"/projects/{project_id}")
        assert page.status_code == 200

    store = app.state.store
    project = store.load(project_id)
    assert project.source is not None
    assert project.source.unit_key == "mock/JHN/001"
    assert project.scenes, "the script ran, so there is something to read at gate 1"

    # Stopped *at* gate 1: scripted, unapproved, and nothing voiced.
    status = derive_status(project, stage_cache_for(store, project_id))
    assert status is Status.SCRIPT_READY
    assert project.approvals.script is None
    assert all(scene.audio_path is None for scene in project.scenes)


def test_pressing_watch_again_picks_the_same_project_back_up(app):
    with TestClient(app) as client:
        first = client.post("/read/mock/JHN/1/watch", follow_redirects=False)
        app.state.jobs.wait_idle(120)
        second = client.post("/read/mock/JHN/1/watch", follow_redirects=False)
        app.state.jobs.wait_idle(120)

        assert second.headers["location"] == first.headers["location"]
        assert len(app.state.store.list_ids()) == 1

        panel = client.get("/read/mock/JHN/1/mode/watch")
        assert "already exists" in panel.text
        assert "Start a video of this passage" not in panel.text


def test_the_watched_project_appears_in_the_workspace_as_a_video(app):
    with TestClient(app) as client:
        client.post("/read/mock/JHN/1/watch", follow_redirects=False)
        app.state.jobs.wait_idle(120)
        body = client.get("/").text

    # Filed under its work, so it is reached through the folder rather than loose.
    assert "mock" in body
    assert 'data-folder="mock"' in body


def test_reading_still_creates_no_project(app):
    with TestClient(app) as client:
        client.get("/read/mock/JHN/1")
        client.get("/read/mock/JHN/1/mode/brief")
        client.get("/read/mock/JHN/1/mode/listen")
        client.post("/read/mock/JHN/1/audio")
        app.state.jobs.wait_idle(120)

    assert app.state.store.list_ids() == [], "only watching crosses into the pipeline"
