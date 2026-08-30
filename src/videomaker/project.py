import fcntl
import os
import re
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from videomaker.models import Project

PROJECTS_DIRNAME = "projects"
PROJECT_FILE = "project.json"
LOCK_FILE = ".lock"
SUBDIRS = ("scenes", "audio", "captions", "build", "output", "cache")
MAX_SLUG_LEN = 60

_NON_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(topic: str) -> str:
    decomposed = unicodedata.normalize("NFKD", topic)
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    slug = _NON_SLUG.sub("-", ascii_only.lower()).strip("-")
    return slug[:MAX_SLUG_LEN].strip("-")


class ProjectStore:
    """Owns the on-disk layout of `<workspace>/projects/<id>/`."""

    def __init__(self, workspace_dir: Path) -> None:
        self.workspace_dir = Path(workspace_dir)
        self.projects_dir = self.workspace_dir / PROJECTS_DIRNAME

    def path_for(self, project_id: str) -> Path:
        return self.projects_dir / project_id

    def list_ids(self) -> list[str]:
        if not self.projects_dir.is_dir():
            return []
        return sorted(p.name for p in self.projects_dir.iterdir() if p.is_dir())

    def _allocate_id(self, topic: str) -> str:
        base = slugify(topic) or "project"
        if not self.path_for(base).exists():
            return base
        n = 2
        while self.path_for(f"{base}-{n}").exists():
            n += 1
        return f"{base}-{n}"

    def create(self, topic: str, template: str, **kw: Any) -> Project:
        project = Project(
            id=self._allocate_id(topic),
            topic=topic,
            template=template,
            created_at=datetime.now(UTC),
            **kw,
        )
        root = self.path_for(project.id)
        for sub in SUBDIRS:
            (root / sub).mkdir(parents=True, exist_ok=True)
        self.save(project)
        return project

    def load(self, project_id: str) -> Project:
        path = self.path_for(project_id) / PROJECT_FILE
        if not path.is_file():
            raise FileNotFoundError(f"no such project: {project_id}")
        return Project.model_validate_json(path.read_text())

    def save(self, project: Project) -> None:
        root = self.path_for(project.id)
        root.mkdir(parents=True, exist_ok=True)
        tmp = root / f"{PROJECT_FILE}.tmp"
        tmp.write_text(project.model_dump_json(indent=2))
        os.replace(tmp, root / PROJECT_FILE)

    def scene_dir(self, project: Project, scene_id: str) -> Path:
        path = self.path_for(project.id) / "scenes" / scene_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    @contextmanager
    def lock(self, project_id: str) -> Iterator[Path]:
        """Exclusive, blocking whole-project lock (Linux `flock`)."""
        root = self.path_for(project_id)
        root.mkdir(parents=True, exist_ok=True)
        path = root / LOCK_FILE
        with path.open("w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield path
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
