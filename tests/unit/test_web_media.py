import pytest

from videomaker.web.media import safe_project_path


@pytest.fixture
def root(tmp_path):
    project = tmp_path / "projects" / "demo"
    (project / "output").mkdir(parents=True)
    (project / "output" / "final_wide.mp4").write_bytes(b"video")
    (tmp_path / "secret.txt").write_text("do not serve me")
    return project


def test_allows_a_file_inside_the_project(root):
    assert safe_project_path(root, "output/final_wide.mp4").read_bytes() == b"video"


@pytest.mark.parametrize(
    "attack",
    [
        "../../secret.txt",
        "../secret.txt",
        "output/../../../secret.txt",
        "/etc/passwd",
        "//etc/passwd",
        "output/./../../secret.txt",
        "....//....//secret.txt",
    ],
)
def test_rejects_traversal(root, attack):
    with pytest.raises(ValueError):
        safe_project_path(root, attack)


def test_rejects_a_symlink_pointing_outside(root, tmp_path):
    (root / "output" / "escape.txt").symlink_to(tmp_path / "secret.txt")
    with pytest.raises(ValueError):
        safe_project_path(root, "output/escape.txt")


def test_rejects_a_sibling_directory_sharing_a_name_prefix(root, tmp_path):
    evil = tmp_path / "projects" / "demo-evil"
    evil.mkdir(parents=True)
    (evil / "loot.txt").write_text("nope")
    # String-prefix containment checks pass this; only real path containment fails it.
    with pytest.raises(ValueError):
        safe_project_path(root, "../demo-evil/loot.txt")


def test_missing_file_raises_file_not_found(root):
    with pytest.raises(FileNotFoundError):
        safe_project_path(root, "output/nope.mp4")


# --- Guard cases beyond the plan's block --------------------------------------
#
# The route hands the guard an already-URL-decoded string, so these are all
# reachable from the wire.


@pytest.mark.parametrize(
    "attack",
    [
        "",
        ".",
        "..",
        "output/",
        "output//final_wide.mp4",
        "..\\secret.txt",
        "output\\..\\..\\secret.txt",
        "output/final_wide.mp4\x00.txt",
        "a/b/../../../../secret.txt",
        "\\\\server\\share\\secret.txt",
    ],
)
def test_rejects_more_traversal_shapes(root, attack):
    with pytest.raises(ValueError):
        safe_project_path(root, attack)


def test_rejects_a_symlinked_directory_pointing_outside(root, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "loot.txt").write_text("nope")
    (root / "output" / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        safe_project_path(root, "output/link/loot.txt")


def test_a_directory_is_not_a_servable_file(root):
    with pytest.raises(FileNotFoundError):
        safe_project_path(root, "output")


def test_a_symlink_staying_inside_the_project_is_allowed(root):
    (root / "output" / "alias.mp4").symlink_to(root / "output" / "final_wide.mp4")
    assert safe_project_path(root, "output/alias.mp4").read_bytes() == b"video"


# --- Route level ---------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    from fastapi.testclient import TestClient

    from videomaker.config import Settings
    from videomaker.web.app import create_app

    project = tmp_path / "projects" / "demo"
    (project / "output").mkdir(parents=True)
    (project / "output" / "final_wide.mp4").write_bytes(b"video")
    (tmp_path / "secret.txt").write_text("do not serve me")
    (tmp_path / "projects" / "demo-evil").mkdir(parents=True)
    (tmp_path / "projects" / "demo-evil" / "loot.txt").write_text("nope")
    return TestClient(create_app(Settings(workspace_dir=tmp_path)))


def test_route_serves_a_file_with_a_sensible_content_type(client):
    response = client.get("/media/demo/output/final_wide.mp4")
    assert response.status_code == 200
    assert response.content == b"video"
    assert response.headers["content-type"].startswith("video/mp4")


# httpx resolves literal `../` segments in a URL before sending, so every wire
# level traversal here is percent-encoded — which is exactly how a real attacker
# reaches the handler, since Starlette decodes before the converter runs.
@pytest.mark.parametrize(
    "url",
    [
        "/media/demo/%2e%2e%2f%2e%2e%2fsecret.txt",
        "/media/demo/output%2f..%2f..%2f..%2fsecret.txt",
        "/media/demo/%2fetc%2fpasswd",
        "/media/demo/....%2f%2f....%2f%2fsecret.txt",
        "/media/demo/a%2fb%2f..%2f..%2f..%2f..%2fsecret.txt",
        "/media/demo/..%5csecret.txt",
        "/media/%2e%2e/%2e%2e/secret.txt",
        "/media/%2e%2e/demo-evil/loot.txt",
        "/media/demo/%252e%252e%252fsecret.txt",
    ],
)
def test_route_returns_404_for_traversal(client, url):
    response = client.get(url)
    assert response.status_code == 404
    assert b"do not serve me" not in response.content
    assert b"nope" not in response.content


def test_route_404_does_not_disclose_paths_outside_the_root(client):
    outside = client.get("/media/demo/%2e%2e%2f%2e%2e%2fsecret.txt")
    missing_inside = client.get("/media/demo/output/nope.mp4")
    unknown_project = client.get("/media/no-such-project/output/final_wide.mp4")
    assert outside.status_code == missing_inside.status_code == 404
    assert unknown_project.status_code == 404
    # An attacker must not be able to tell "outside the root" from "not there".
    assert outside.json() == missing_inside.json() == unknown_project.json()
