"""One list over two stores: what is open in this workspace, whatever kind it is.

**A reading is still not a `Project`.** `Project` means the video pipeline —
`STAGE_ORDER`, three gates, `derive_status` — and putting a chapter of John
through it would let the reader stale a stage or trip a gate. So this module is a
*projection*: it reads `ProjectStore` and the library, and yields one row type the
root page can render without knowing which store a row came from. Nothing here
writes, and nothing here is stored.

The two shapes answer the same three questions in different words, which is what
makes the projection worth having rather than a template with two loops:

* what is it called — a topic, or a title;
* where do I go — a project page, or a work's outline;
* does it want me — a project waiting at a gate does; a work never does, because
  a work has no gates and asks nothing of anybody.
"""

from enum import StrEnum

from pydantic import BaseModel

from videomaker.config import Settings
from videomaker.corpus.importer import list_works
from videomaker.models import Status
from videomaker.project import ProjectStore
from videomaker.runner import derive_status, stage_cache_for

#: The status a project derives to while each gate is still unapproved.
#: `derive_status` stops *at* an unapproved gate, so the status it returns is the
#: one below it — this table is that correspondence, and it is the only thing here
#: that knows the gate names. Keyed by gate so a fourth gate is one line.
STATUS_BEFORE_GATE: dict[str, Status] = {
    "script": Status.SCRIPT_READY,
    "storyboard": Status.STORYBOARD_READY,
    "preview": Status.PREVIEW_READY,
}

_GATE_FOR_STATUS: dict[Status, str] = {
    status: gate for gate, status in STATUS_BEFORE_GATE.items()
}


class Kind(StrEnum):
    VIDEO = "video"
    READING = "reading"


class WorkspaceItem(BaseModel):
    """One thing open in the workspace, whichever store it lives in."""

    kind: Kind
    id: str
    title: str
    href: str
    #: What this row is doing right now, in the words that row's kind uses.
    detail: str
    #: The palette's own vocabulary — `""`, `"warn"`, `"ok"` — so the template
    #: picks a class rather than inventing a colour.
    tone: str = ""
    #: True only when a human is the thing standing between this and the next
    #: step. A work is never `needs_you`: there is nothing to approve in reading.
    needs_you: bool = False
    #: Newest first. Seconds since the epoch; a work with no timestamp sorts last
    #: rather than pretending to be new.
    sort_key: float = 0.0
    #: The folder label filing this row away from the root, `""` for the root
    #: itself. A work is always at the root: the library has no folders, and
    #: inventing some to match would be a feature nobody asked for.
    folder: str = ""
    #: The raw derived status of a video project, for the row's `data-status`.
    #: Empty for a reading, which has no status to derive.
    status: str = ""


def _project_items(store: ProjectStore) -> list[WorkspaceItem]:
    """Every readable project. One that will not load is skipped, not raised —
    the workspace is a folder the user is invited to poke at, and one half-written
    `project.json` must not take the root page down."""
    items: list[WorkspaceItem] = []
    for project_id in store.list_ids():
        try:
            project = store.load(project_id)
        except (OSError, ValueError):
            continue
        status = derive_status(project, stage_cache_for(store, project_id))
        gate = _gate_ahead(project, status)
        items.append(
            WorkspaceItem(
                kind=Kind.VIDEO,
                id=project.id,
                title=project.topic,
                href=f"/projects/{project.id}",
                detail=f"waiting at the {gate} gate" if gate else status.value.replace("_", " "),
                tone="warn" if gate else ("ok" if status is Status.RENDERED else ""),
                needs_you=gate is not None,
                sort_key=project.created_at.timestamp(),
                folder=project.folder,
                status=status.value,
            )
        )
    return items


def _gate_ahead(project, status: Status) -> str | None:
    """Which gate is holding this project up, or None if none is.

    A status in the table is *necessary* but not sufficient: a project that has
    been through gate 2 and is mid-storyboard also derives `storyboard_ready`,
    and it is not waiting for anybody. The approval stamp is what settles it.
    """
    gate = _GATE_FOR_STATUS.get(status)
    if gate is None:
        return None
    return gate if getattr(project.approvals, gate) is None else None


def _work_items(settings: Settings) -> list[WorkspaceItem]:
    """Every readable work in the library, with its size rather than its state.

    A work has no state to report: it is imported or it is not. So the detail is
    the one number a reader actually wants before opening it.
    """
    items: list[WorkspaceItem] = []
    for work, chapters in list_works(settings.workspace_dir):
        items.append(
            WorkspaceItem(
                kind=Kind.READING,
                id=work.id,
                title=work.title,
                href=f"/library/{work.id}",
                detail=f"{chapters} chapters" if chapters != 1 else "1 chapter",
                sort_key=_imported_at(settings, work.id),
            )
        )
    return items


def _imported_at(settings: Settings, work_id: str) -> float:
    """When the work was last imported, from `work.yaml`'s mtime.

    The `WorkRef` carries no timestamp and adding one would rewrite every
    `work.yaml` on disk for a sort order; the file's own mtime is already the
    answer and costs one `stat`.
    """
    from videomaker.corpus.importer import WORK_FILE, work_dir

    try:
        return (work_dir(settings.workspace_dir, work_id) / WORK_FILE).stat().st_mtime
    except OSError:
        return 0.0


def workspace_items(settings: Settings, store: ProjectStore) -> list[WorkspaceItem]:
    """Everything open in this workspace, newest first, whatever kind it is."""
    items = [*_project_items(store), *_work_items(settings)]
    items.sort(key=lambda item: item.sort_key, reverse=True)
    return items


def waiting(items: list[WorkspaceItem]) -> list[WorkspaceItem]:
    """The ones standing still until a human looks at them — every folder included.

    Counted across the whole workspace rather than across what the root happens to
    be showing: a project filed in a folder still needs you, and a summary that
    only counted the visible ones would teach you to distrust it.
    """
    return [item for item in items if item.needs_you]


def at_root(items: list[WorkspaceItem]) -> list[WorkspaceItem]:
    """The ones the root lists itself: everything not filed inside a folder.

    A project in a folder is reached *through* the folder card, so printing it
    here as well would undo the whole drill-down.
    """
    return [item for item in items if not item.folder]


def counts(items: list[WorkspaceItem]) -> dict[str, int]:
    """How many of each kind, for a page that says what it is showing."""
    return {
        kind.value: sum(1 for item in items if item.kind is kind) for kind in Kind
    }
