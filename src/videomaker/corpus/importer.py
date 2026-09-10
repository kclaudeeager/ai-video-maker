"""Bringing a text into the library, behind the licence gate.

**The gate is the point of this module.** ebible.org has no site-wide licence;
each translation's own page is the authority, and "public domain" for one text says
nothing about the next. So an `ImportSpec` without a `licence`, a `licence_url`
and a `source_url` does not validate, and `import_work` writes nothing — not a
directory, not a partial file — when the work it would record fails validation.
There is no `--force` and none may be added (`/CLAUDE.md`, rule 4).

On disk, under `<workspace>/library/<work_id>/`:

    work.yaml            the WorkRef, verbatim
    source/              the USFM exactly as downloaded. Never edited.
    units/<BOOK>/<NNN>.json     UnitText, derived from source/
    derived/             briefs and audio, cache-keyed and disposable

A re-import rebuilds `source/` and `units/` in a staging directory and swaps them
in, and leaves `derived/` alone: the briefs and readings there are keyed by the
text they were made from, so an unchanged chapter keeps its audio and a changed
one stops matching on its own.
"""

import os
import shutil
import zipfile
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError

from videomaker.cache import _write_atomic
from videomaker.corpus.models import CHAPTER_DIGITS, UnitRef, UnitText, Versification, WorkRef
from videomaker.corpus.usfm import iter_chapters
from videomaker.downloads import download_file

LIBRARY_DIRNAME = "library"
WORK_FILE = "work.yaml"
SOURCE_DIRNAME = "source"
UNITS_DIRNAME = "units"
DERIVED_DIRNAME = "derived"
#: Built here, swapped in whole, so an interrupted import leaves the previous
#: `source/` and `units/` intact rather than half of each.
STAGING_DIRNAME = ".staging"

#: The repository's third-party notices. Relative to the working directory, like
#: `config.DEFAULT_CONFIG_FILE`, because the CLI is run from the repository root;
#: when it is not there — an installed wheel, another directory — nothing is
#: appended, because creating a NOTICE.md in a stranger's directory is worse than
#: leaving one line out of ours.
NOTICE_PATH = Path("NOTICE.md")
NOTICE_SECTION = "## Bundled texts"

_USFM_SUFFIXES = frozenset({".usfm", ".sfm"})


class ImportSpec(BaseModel):
    work_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    title: str
    language: str
    licence: str = Field(min_length=1)
    licence_url: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    versification: Versification = Versification.KJV
    #: URL or local path to a USFM zip, or a local directory of USFM files.
    archive: str | None = None

    def work(self) -> WorkRef:
        return WorkRef(
            id=self.work_id,
            title=self.title,
            language=self.language,
            licence=self.licence,
            licence_url=self.licence_url,
            source_url=self.source_url,
            versification=self.versification,
        )


# ------------------------------------------------------------------------ layout


def library_dir(root: Path) -> Path:
    return Path(root) / LIBRARY_DIRNAME


def work_dir(root: Path, work_id: str) -> Path:
    return library_dir(root) / work_id


def unit_path(work_root: Path, ref: UnitRef) -> Path:
    return Path(work_root) / UNITS_DIRNAME / ref.book / f"{ref.chapter:0{CHAPTER_DIGITS}d}.json"


def read_work(work_root: Path) -> WorkRef:
    """The `work.yaml` of one library entry. Raises on a missing or invalid one."""
    return WorkRef.model_validate(yaml.safe_load((Path(work_root) / WORK_FILE).read_text()))


def list_works(root: Path) -> list[tuple[WorkRef, int]]:
    """Every readable work in the library with its chapter count, in id order.

    A directory without a valid `work.yaml` is skipped rather than raised: one
    interrupted import must not take `library list` down with it.
    """
    rows: list[tuple[WorkRef, int]] = []
    library = library_dir(root)
    if not library.is_dir():
        return rows
    for entry in sorted(library.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        try:
            work = read_work(entry)
        except (OSError, ValueError, ValidationError):
            continue
        chapters = sum(1 for _ in (entry / UNITS_DIRNAME).glob("*/*.json"))
        rows.append((work, chapters))
    return rows


# ------------------------------------------------------------------------ source


def _is_url(archive: str) -> bool:
    return archive.startswith(("http://", "https://"))


def _fetch_source(archive: str, source: Path) -> None:
    """Fill `source/` with the archive's USFM files, flat, as they came.

    Three shapes are accepted because three are what exist: ebible and Berean
    publish zips over HTTP, a zip may already be on disk, and the test fixture is
    a bare directory. Zip members are flattened to their basename — every archive
    seen so far is flat already, and a nested one would otherwise put a path
    separator into a filename we later open.
    """
    source.mkdir(parents=True)
    if _is_url(archive):
        path = download_file(archive, source.parent / "archive.zip")
    else:
        path = Path(archive).expanduser()
    if path.is_dir():
        for file in sorted(path.iterdir()):
            if file.suffix.lower() in _USFM_SUFFIXES:
                shutil.copyfile(file, source / file.name)
        return
    if not path.is_file():
        raise FileNotFoundError(f"no archive at {archive!r}")
    with zipfile.ZipFile(path) as archive_file:
        for member in archive_file.infolist():
            name = Path(member.filename).name
            if member.is_dir() or Path(name).suffix.lower() not in _USFM_SUFFIXES:
                continue
            with archive_file.open(member) as src, (source / name).open("wb") as dst:
                shutil.copyfileobj(src, dst)
    if _is_url(archive):
        path.unlink()


def _derive_units(work_id: str, source: Path, units: Path) -> int:
    """`source/*.usfm` -> `units/<BOOK>/<NNN>.json`. Returns the chapter count.

    Front matter and glossary files hold no chapter and so write nothing; that is
    the whole of the skip logic, and it is why there is no list of filenames to
    keep in step with each publisher's naming.
    """
    count = 0
    for file in sorted(source.iterdir()):
        text = file.read_text(encoding="utf-8-sig")
        for unit in iter_chapters(text):
            stamped = UnitText(
                ref=unit.ref.model_copy(update={"work_id": work_id}),
                title=unit.title,
                verses=unit.verses,
            )
            _write_atomic(unit_path(units.parent, stamped.ref), stamped.model_dump_json(indent=1))
            count += 1
    return count


def _swap_in(staged: Path, live: Path) -> None:
    if live.exists():
        shutil.rmtree(live)
    os.replace(staged, live)


def _note_in_notice(work: WorkRef) -> None:
    """One line per work under `NOTICE_SECTION`, creating the section, never the file."""
    if not NOTICE_PATH.is_file():
        return
    text = NOTICE_PATH.read_text()
    marker = f"(`{work.id}`,"
    if marker in text:
        return
    line = (
        f"- **{work.title}** {marker} {work.language}) — {work.licence} "
        f"Licence: {work.licence_url}. Source: {work.source_url}. Imported into the "
        f"user's own `workspace/library/`; not in the repository."
    )
    if NOTICE_SECTION not in text:
        text = f"{text.rstrip()}\n\n{NOTICE_SECTION}\n"
    text = text.rstrip()
    gap = "\n\n" if text.endswith(NOTICE_SECTION) else "\n"
    _write_atomic(NOTICE_PATH, f"{text}{gap}{line}\n")


# ------------------------------------------------------------------------ import


def set_published(work_id: str, published: bool, root: Path) -> WorkRef:
    """Publish a work, or take it back. Rewrites `work.yaml` and nothing else.

    Deliberately not part of `ImportSpec`: importing a text and putting it in
    front of other people are two decisions, and conflating them would mean every
    import published. Re-importing keeps whatever was set, because `import_work`
    reads the flag back off disk before it writes.
    """
    target = work_dir(root, work_id)
    work = read_work(target).model_copy(update={"published": published})
    _write_atomic(target / WORK_FILE, yaml.safe_dump(work.model_dump(mode="json"), sort_keys=False))
    return work


def import_work(spec: ImportSpec, root: Path) -> WorkRef:
    """Fetch, parse and file one work. Returns the `WorkRef` written to `work.yaml`.

    The licence gate runs first, before any path exists: `spec.work()` re-validates
    through `WorkRef`, so a spec built with `model_construct` — or by a future
    caller that forgot — is still refused with nothing on disk to clean up.
    """
    work = spec.work()
    # A re-import must not quietly unpublish. `ImportSpec` has no `published`
    # field — publishing is a separate decision — so the flag is read back off
    # the work already on disk, if there is one.
    try:
        work = work.model_copy(update={"published": read_work(work_dir(root, work.id)).published})
    except (OSError, ValueError, ValidationError):
        pass
    if spec.archive is None:
        raise ValueError(f"{spec.work_id!r} has no archive; pass --from <url|path>")
    target = work_dir(root, work.id)
    staging = target / STAGING_DIRNAME
    if staging.exists():
        shutil.rmtree(staging)
    try:
        _fetch_source(spec.archive, staging / SOURCE_DIRNAME)
        count = _derive_units(work.id, staging / SOURCE_DIRNAME, staging / UNITS_DIRNAME)
        if count == 0:
            raise ValueError(f"{spec.archive!r} holds no chapter with a verse in it")
        _swap_in(staging / SOURCE_DIRNAME, target / SOURCE_DIRNAME)
        _swap_in(staging / UNITS_DIRNAME, target / UNITS_DIRNAME)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    (target / DERIVED_DIRNAME).mkdir(exist_ok=True)
    _write_atomic(target / WORK_FILE, yaml.safe_dump(work.model_dump(mode="json"), sort_keys=False))
    _note_in_notice(work)
    return work
