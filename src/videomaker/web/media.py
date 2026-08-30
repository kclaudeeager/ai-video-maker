"""Serving project files over HTTP, and the path guard that makes it safe.

The server has no authentication (by design: it binds `127.0.0.1`) and this
route turns a user-supplied string into a filesystem read. A traversal bug here
is therefore an arbitrary-file-read of the whole machine, so the guard —
`safe_project_path` — is the point of this module and the route is a thin shell
around it.

Two independent layers have to agree before a byte is served:

1. A *syntactic* layer that rejects the request shape outright: absolute paths,
   backslashes, NUL, empty segments, and any segment that is nothing but dots
   (`.`, `..`, and the `....//` filter-evasion shape). Nothing legitimate in a
   project tree needs those, and rejecting them early means the resolver is
   never handed a hostile string in the first place.
2. A *semantic* layer that joins, calls `Path.resolve()` — which follows
   symlinks, so a link pointing out of the tree is caught — and then confirms
   containment with `Path.is_relative_to`.

Layer 2 is deliberately never a string prefix comparison: `.../projects/demo-evil`
starts with `.../projects/demo` as a string while being an entirely different
directory. `is_relative_to` compares path components, so it says no.
"""

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

#: Path segments that are only dots. Covers `.` and `..` and also `....`, the
#: shape used against sanitisers that strip `../` once and non-recursively.
_DOTS_ONLY = re.compile(r"\A\.+\Z")

#: Both separators, because the decoded path may carry Windows-style ones and we
#: would rather reject them than let a future non-POSIX host reinterpret them.
_SEPARATORS = re.compile(r"[/\\]")


def _checked_segments(relative: str) -> list[str]:
    """Split `relative` into segments, rejecting anything hostile or ambiguous.

    Raises `ValueError` for absolute paths, NUL bytes, empty segments (`a//b`,
    and the leading empty segment of `/etc/passwd`), and dots-only segments.
    """
    if not relative:
        raise ValueError("empty path")
    if "\x00" in relative:
        raise ValueError("path contains a NUL byte")
    if relative.startswith(("/", "\\")):
        raise ValueError("absolute paths are not accepted")

    segments = _SEPARATORS.split(relative)
    for segment in segments:
        if not segment:
            raise ValueError("path contains an empty segment")
        if _DOTS_ONLY.match(segment):
            raise ValueError("path contains a dots-only segment")
    return segments


def _resolve_within(root: Path, relative: str) -> Path:
    """Join `relative` onto `root` and prove the result is still inside it.

    Raises `ValueError` on any escape. Both sides are resolved, so symlinks —
    in the candidate *and* in `root` itself — are followed before comparison.
    """
    segments = _checked_segments(relative)
    resolved_root = Path(root).resolve()
    candidate = resolved_root.joinpath(*segments).resolve()
    if not candidate.is_relative_to(resolved_root):
        raise ValueError("path escapes the project root")
    return candidate


def safe_project_path(root: Path, relative: str) -> Path:
    """Resolve `relative` inside project directory `root`.

    Raises `ValueError` if the path escapes `root` by any route, and
    `FileNotFoundError` if it stays inside but is not a readable file (missing,
    or a directory — neither is servable).
    """
    candidate = _resolve_within(root, relative)
    if not candidate.is_file():
        raise FileNotFoundError("no such file in this project")
    return candidate


def project_root(projects_dir: Path, project_id: str) -> Path:
    """Resolve `project_id` to a project directory inside `projects_dir`.

    The id is user-supplied too — a `{project_id}` path parameter cannot contain
    a slash, but `..` reaches the handler happily — so it goes through the same
    guard. Raises `ValueError` on escape, `FileNotFoundError` if absent.
    """
    root = _resolve_within(projects_dir, project_id)
    if not root.is_dir():
        raise FileNotFoundError("no such project")
    return root


router = APIRouter()

#: One message for every failure mode. Distinguishing "outside the root" from
#: "not there" would turn 404s into an oracle for probing the filesystem.
_NOT_FOUND = "not found"


@router.get("/media/{project_id}/{path:path}")
def get_media(request: Request, project_id: str, path: str) -> FileResponse:
    """Serve one file from a project directory.

    Starlette's `{path:path}` converter does not sanitise, and what arrives here
    is already URL-decoded, so `path` is fully attacker-controlled; the guard is
    the only thing between it and the filesystem. Every rejection is a 404.
    """
    store = request.app.state.store
    try:
        target = safe_project_path(project_root(store.projects_dir, project_id), path)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=_NOT_FOUND) from exc
    return FileResponse(target)
