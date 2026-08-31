"""Reclaiming disk space — the only place in this codebase that deletes files.

M1 measured 1–3 GB of intermediates per project and `doctor` warns under 20 GB
free, so a project that has been rendered is mostly rebuildable bulk. This module
works out exactly which files those are, how much they weigh, and removes them.

**Everything here is arranged around one asymmetry.** A missed file costs a few
hundred megabytes; a file deleted by mistake costs hours of compute and provider
quota that may not come back. So the module never asks "is this safe to delete?"
of an arbitrary path. It *collects* from three named places and then, immediately
before unlinking, re-derives the answer from scratch in `is_removable`:

1. `build/` — segments, concat lists, timelines, narration beds, previews. Pure
   intermediates: `assemble` rebuilds all of it from artefacts that live
   elsewhere, so removing it never re-runs `script`, `voice` or `align`.
2. `scenes/<id>/asset.*` — downloaded footage. Rebuildable, but only by spending
   provider quota, which is why it takes `--all` to reach it. Named by stem, so
   the take (`narration.wav`) and its alignment (`words.json`) sitting in the
   same folder are not merely spared — they are not collectible in the first
   place.
3. `output/` — the deliverables. Reached only by an explicit `--all` without
   `--keep-outputs`.

`project.json` is in none of those, and `is_removable` refuses it by name on top
of that: losing it loses the project regardless of what else is on disk. So is
`cache/stages.json`, which is a few kilobytes of ledger whose loss would re-run
the LLM and the TTS for every scene — the worst space-to-cost ratio on disk.
Symlinks are never collected and never followed: a link in `build/` is not an
intermediate, and whatever it points at is not ours to delete.

**Containment.** A project id is user input. It is resolved by
`web.media.project_root` — the audited guard from M2 — rather than by a second
implementation here, because two copies of a path guard is one copy too many.
That guard resolves symlinks and compares with `Path.is_relative_to`, never a
string prefix: `<workspace>/projects-evil` starts with `<workspace>/projects` as
a string while being an entirely different directory.

**Staleness.** Deleting an artefact does not, on its own, make the status view
say so: `runner` derives `assemble`/`render` from fields in `project.json`, not
from the disk. `apply_clean` therefore drops the stage-cache entries the removed
files backed, which is what makes `videomaker status` report those stages pending
and what a re-run reads to know it has work to do. Nothing here writes
`project.json`.
"""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from videomaker.pipeline.align import WORDS_FILENAME
from videomaker.pipeline.assemble import BUILD_DIRNAME
from videomaker.pipeline.render import OUTPUT_DIRNAME
from videomaker.pipeline.visuals import ASSET_STEM
from videomaker.pipeline.voice import NARRATION_FILENAME
from videomaker.project import LOCK_FILE, PROJECT_FILE, ProjectStore
from videomaker.runner import STAGES_RELPATH, STATUS_PREFIX, stage_cache_for

#: The three things `clean` can remove, as category keys.
BUILD = "build"
ASSETS = "assets"
OUTPUT = "output"

#: What each category is called in the printed plan. `build/` and `output/` are
#: whole folders; the asset glob is written out because it is deliberately narrow.
LABELS = {
    BUILD: f"{BUILD_DIRNAME}/",
    ASSETS: f"scenes/*/{ASSET_STEM}.*",
    OUTPUT: f"{OUTPUT_DIRNAME}/",
}

#: Names that are never removable wherever they are found. Belt to the braces of
#: `is_removable`'s area check: none of these lives in a collectible area today,
#: and if one ever moves into one it still will not go.
PROTECTED_NAMES = frozenset(
    {PROJECT_FILE, LOCK_FILE, NARRATION_FILENAME, WORDS_FILENAME, Path(STAGES_RELPATH).name}
)

#: Stage-cache key prefixes that stop meaning anything once a category is gone.
#: `preview` covers `preview-mix` too, which is the intent: both are `build/`.
STALE_AFTER: dict[str, tuple[str, ...]] = {
    BUILD: ("assemble", "preview"),
    ASSETS: ("visuals", "assemble", "preview"),
    OUTPUT: ("render", "thumbnail"),
}

_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


class UnknownProject(ValueError):
    """The id names nothing inside the workspace — missing, or trying to escape it.

    One exception for both, phrased one way, for the same reason `web.media` gives
    one 404: telling the difference turns a refusal into a probe of the filesystem.
    """


def human_bytes(size: int) -> str:
    """Bytes as a person reads them. Whole bytes below 1 KB, one decimal above."""
    value = float(size)
    for unit in _UNITS[:-1]:
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} {_UNITS[-1]}"


# --------------------------------------------------------------------- the plan


@dataclass(frozen=True)
class Group:
    """One project's files in one category — the unit the plan is printed in."""

    project_id: str
    root: Path
    category: str
    label: str
    paths: tuple[Path, ...]
    total_bytes: int


@dataclass(frozen=True)
class CleanPlan:
    """Everything a single `clean` invocation would remove, and nothing else."""

    groups: tuple[Group, ...]

    def __bool__(self) -> bool:
        return bool(self.groups)

    @property
    def file_count(self) -> int:
        return sum(len(group.paths) for group in self.groups)

    @property
    def total_bytes(self) -> int:
        return sum(group.total_bytes for group in self.groups)

    def by_project(self) -> Iterator[tuple[str, list[Group]]]:
        """Groups gathered per project, in plan order — one lock, one ledger write."""
        seen: dict[str, list[Group]] = {}
        for group in self.groups:
            seen.setdefault(group.project_id, []).append(group)
        yield from seen.items()


# ----------------------------------------------------------------- the guards


def project_root_for(store: ProjectStore, project_id: str) -> Path:
    """Resolve `project_id` to a directory proven to be inside the workspace.

    Delegates to the M2 guard rather than re-deriving it: `web.media.project_root`
    rejects the hostile shapes syntactically, resolves symlinks, and confirms
    containment with `Path.is_relative_to`.
    """
    from videomaker.web.media import project_root

    try:
        return project_root(store.projects_dir, project_id)
    except (ValueError, FileNotFoundError) as exc:
        raise UnknownProject(f"no such project: {project_id}") from exc


def is_removable(root: Path, path: Path) -> bool:
    """True only for a real file inside one of the three collectible areas.

    Re-derived from the path itself rather than trusted from the collector, so a
    plan that grew a path by any other route — a bug, a caller, a future feature —
    still cannot get it unlinked.
    """
    if path.name in PROTECTED_NAMES:
        return False
    try:
        parts = path.resolve().relative_to(Path(root).resolve()).parts
    except ValueError:
        return False
    if not parts:
        return False
    if parts[0] in (BUILD_DIRNAME, OUTPUT_DIRNAME):
        return True
    return len(parts) == 3 and parts[0] == "scenes" and parts[2].startswith(f"{ASSET_STEM}.")


def check_plan(plan: CleanPlan) -> None:
    """Prove every path in `plan` is removable, before a single one is unlinked.

    Whole-plan rather than per-path so a bad entry cannot be reached *after* good
    ones have already gone: the refusal has to leave the disk untouched.
    """
    for group in plan.groups:
        for path in group.paths:
            if not is_removable(group.root, path):
                raise ValueError(f"refusing to remove {path}: not a rebuildable project file")


# ------------------------------------------------------------- collecting them


def _files_under(directory: Path) -> list[Path]:
    """Every real file in `directory`, symlinks excluded (see the module docstring)."""
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.rglob("*") if p.is_file() and not p.is_symlink())


def _asset_files(root: Path) -> list[Path]:
    """Downloaded footage only: `scenes/<id>/asset.*`, by stem, one level down."""
    return sorted(
        p
        for p in (root / "scenes").glob(f"*/{ASSET_STEM}.*")
        if p.is_file() and not p.is_symlink()
    )


COLLECTORS = {
    BUILD: lambda root: _files_under(root / BUILD_DIRNAME),
    ASSETS: _asset_files,
    OUTPUT: lambda root: _files_under(root / OUTPUT_DIRNAME),
}


def _group(project_id: str, root: Path, category: str) -> Group | None:
    paths = [p for p in COLLECTORS[category](root) if is_removable(root, p)]
    if not paths:
        return None
    return Group(
        project_id=project_id,
        root=root,
        category=category,
        label=LABELS[category],
        paths=tuple(paths),
        total_bytes=sum(p.stat().st_size for p in paths),
    )


def plan_clean(
    store: ProjectStore,
    project_ids: Iterable[str],
    *,
    remove_assets: bool = False,
    remove_outputs: bool = False,
) -> CleanPlan:
    """What `clean` would remove for these projects. Reads the disk, changes nothing.

    `build/` is always in; the other two are opt-in and independent, so
    `--all --keep-outputs` is a real combination and not a special case.
    """
    categories = [BUILD]
    if remove_assets:
        categories.append(ASSETS)
    if remove_outputs:
        categories.append(OUTPUT)

    groups: list[Group] = []
    for project_id in project_ids:
        root = project_root_for(store, project_id)
        groups += [g for g in (_group(project_id, root, c) for c in categories) if g is not None]
    plan = CleanPlan(groups=tuple(groups))
    check_plan(plan)
    return plan


# ------------------------------------------------------------------ removing


def _stale_prefixes(groups: Iterable[Group]) -> list[str]:
    return sorted({prefix for group in groups for prefix in STALE_AFTER[group.category]})


def _drop_stage_entries(store: ProjectStore, project_id: str, groups: Iterable[Group]) -> None:
    """Forget the cache entries the removed files backed, status entries included.

    Both halves are needed: the stage half is what a re-run consults to decide it
    has work to do, and the `status:` half is what `derive_status` reads — which is
    what makes `videomaker status` say `pending` for a stage whose output has gone.
    """
    cache = stage_cache_for(store, project_id)
    for prefix in _stale_prefixes(groups):
        cache.invalidate(prefix)
        cache.invalidate(f"{STATUS_PREFIX}:{prefix}")
    cache.save()


def apply_clean(store: ProjectStore, plan: CleanPlan) -> int:
    """Remove everything in `plan` and return the bytes reclaimed.

    Validated whole, then applied per project under the same `flock` the runner
    takes, so `clean` cannot pull a segment out from under a render in progress.
    """
    check_plan(plan)
    reclaimed = 0
    for project_id, groups in plan.by_project():
        with store.lock(project_id):
            for group in groups:
                for path in group.paths:
                    path.unlink(missing_ok=True)
            reclaimed += sum(group.total_bytes for group in groups)
            _drop_stage_entries(store, project_id, groups)
    return reclaimed
