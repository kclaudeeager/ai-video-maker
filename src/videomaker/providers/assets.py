"""Shared helpers for providers that write asset files into a project folder."""

from pathlib import Path

PROJECT_FILE = "project.json"


def project_relative(out_path: Path) -> str:
    """`AssetRef.local_path` is always relative to the project folder.

    The project folder is the nearest ancestor holding a `project.json`. Outside a
    project (unit tests writing into a bare tmp dir) fall back to the bare filename,
    which is still a valid relative path.
    """
    path = Path(out_path).resolve()
    for parent in path.parents:
        if (parent / PROJECT_FILE).is_file():
            return path.relative_to(parent).as_posix()
    return path.name
