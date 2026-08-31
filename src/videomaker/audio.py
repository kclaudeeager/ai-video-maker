"""The user's own music and SFX library: the scan, the licence record, the index.

**The project ships no audio files, ever.** That is a licensing decision before it
is an architectural one, and `docs/audio-design.md` states it as binding: Content
ID issues false claims against Creative Commons music routinely, and a claim on a
monetised video is exactly the outcome this project exists to avoid. Shipping
nothing means the project can never be the source of one. Everything under
`assets/music/` and `assets/sfx/` is dropped in by the owner of the clone, and
`.gitignore` keeps it out of the repository.

Three consequences shape this module:

* **An empty library is the default, not an error.** `assets/music/` is empty on
  every fresh clone, so "no music" is the path the code takes most often. Nothing
  here raises because the library is empty, missing, or full of files ffprobe
  cannot read; the pipeline renders narration only, exactly as it does today.
* **The index is a cache, never the source of truth.** `music_index.json` exists
  purely to save an ffprobe. The filesystem decides which files exist; a cached
  duration is used only when the file's size *and* mtime still match the bytes it
  was measured from. A missing, stale or corrupt index costs time, never accuracy.
* **`library.yaml` is the licensing record**, and it is what lets M5 generate a
  correct attribution block, the same way Pexels attribution already flows from
  `AssetRef.attribution`. A file with no entry is perfectly usable — it is only
  *reported* as unattributed, by `videomaker music` and `videomaker doctor`.

Mixing, ducking and loudness are Task 9; SFX placement is Task 10. This module
only knows what is on disk and who owns it.
"""

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from videomaker.media.ffmpeg import FFmpegError, probe_duration

MUSIC_KIND = "music"
SFX_KIND = "sfx"

#: The SFX roles the design names, in the order they are used editorially.
SFX_ROLES: tuple[str, ...] = ("transition", "accent", "riser", "ambient")

#: The moods the repo creates as an example, and the values `Template.music_mood`
#: takes today. Nothing enforces this list: a mood is just a directory name, so a
#: user inventing `assets/music/tense/` gets `music_mood: tense` for free.
EXAMPLE_MOODS: tuple[str, ...] = ("calm", "upbeat", "dramatic")

#: Containers ffprobe reads and FFmpeg decodes. Anything else in the tree — the
#: READMEs, `library.yaml`, `.gitkeep`, a stray sleeve image — is not audio.
AUDIO_SUFFIXES = frozenset(
    {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".aiff", ".aif", ".wma"}
)

LIBRARY_FILENAME = "library.yaml"
INDEX_FILENAME = "music_index.json"

#: Bumped whenever an entry's meaning changes. An index written by a different
#: version is discarded rather than migrated — it is a cache; rebuilding is cheap.
INDEX_VERSION = 1

#: Same user-wide cache directory as the response cache and the quota ledger
#: (`runner.USER_CACHE_DIR`), spelled out rather than imported: `render.py` will
#: import this module in Task 9, and `runner` imports `render`.
DEFAULT_INDEX_PATH = Path.home() / ".cache" / "ai-video-maker" / INDEX_FILENAME

#: The keys `library.yaml` understands, in the order a credit line composes them.
RECORD_FIELDS = ("title", "artist", "licence", "attribution", "source_url")


# ------------------------------------------------------------------ the records


@dataclass(frozen=True)
class Attribution:
    """One track's entry in `library.yaml` — the licensing record for that file."""

    title: str = ""
    artist: str = ""
    licence: str = ""
    attribution: str = ""
    source_url: str = ""

    @classmethod
    def from_record(cls, record: dict) -> "Attribution":
        return cls(**{key: str(record.get(key, "") or "") for key in RECORD_FIELDS})

    def credit_line(self) -> str:
        """The line M5 will put in the video description.

        `attribution` wins outright when it is set, because some licences dictate
        the exact wording and a composed string would be wrong. Otherwise the
        pieces are joined into something a human can check at a glance.
        """
        if self.attribution:
            return self.attribution
        parts = [
            f'"{self.title}"' if self.title else "",
            f"by {self.artist}" if self.artist else "",
            f"({self.licence})" if self.licence else "",
        ]
        line = " ".join(part for part in parts if part)
        if self.source_url:
            line = f"{line} — {self.source_url}" if line else self.source_url
        return line


def _clean_key(raw: object) -> str:
    """A `library.yaml` key, tidied but not otherwise interpreted."""
    return str(raw).strip().replace("\\", "/").removeprefix("./")


def lookup_record(records: dict[str, Attribution], key: str) -> str:
    """The `records` key that credits track `key`, or `""`.

    Track keys are `kind/group/file` — `music/calm/rain.wav`. But `library.yaml`
    lives *inside* `assets/music/`, so `calm/rain.wav` is the obvious thing to
    type there and is accepted as well. Rejecting it would not fail loudly: the
    entry would simply match nothing and the track would be reported as
    unattributed, which is the one outcome the record exists to prevent.
    """
    if key in records:
        return key
    short = key.removeprefix(f"{MUSIC_KIND}/")
    return short if short != key and short in records else ""


def read_records(path: Path) -> tuple[dict[str, Attribution], str]:
    """Read `library.yaml`, returning `(records by track key, warning)`.

    Never raises. A missing file is simply an empty record set with no warning —
    that is a fresh clone. A file that exists but cannot be understood *is*
    warned about, because someone meant it to say something.
    """
    path = Path(path)
    try:
        raw = path.read_text()
    except OSError:
        return {}, ""
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        first = str(exc).splitlines()[0] if str(exc) else "unparseable"
        return {}, f"{path} is not valid YAML ({first}); nothing is credited from it"
    if data is None:
        return {}, ""
    if not isinstance(data, dict) or not isinstance(data.get("tracks", {}), dict):
        return {}, f"{path} has no `tracks:` mapping; nothing is credited from it"

    records: dict[str, Attribution] = {}
    for raw_key, record in (data.get("tracks") or {}).items():
        key = _clean_key(raw_key)
        if not key:
            continue
        records[key] = Attribution.from_record(record if isinstance(record, dict) else {})
    return records, ""


# ------------------------------------------------------------------ the library


@dataclass(frozen=True)
class Track:
    """One audio file on disk, plus whatever `library.yaml` says about it."""

    key: str  # "music/calm/rain.wav" — kind, then the path under that kind's directory
    kind: str  # MUSIC_KIND | SFX_KIND
    group: str  # the mood (music) or role (sfx); "" for a file loose at the top
    path: Path
    duration_s: float = 0.0
    size_bytes: int = 0
    mtime_ns: int = 0
    attribution: Attribution | None = None
    probe_error: str = ""

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def attributed(self) -> bool:
        return self.attribution is not None


@dataclass(frozen=True)
class Library:
    """Everything the user has dropped into `assets/`, as of this scan.

    `probed` and `reused` are the index's report card: `probed` counts the files
    the cache could not answer for, so a non-zero count after a scan against an
    existing index means that index was stale.
    """

    tracks: tuple[Track, ...] = ()
    orphan_records: tuple[str, ...] = ()
    record_warning: str = ""
    probed: int = 0
    reused: int = 0

    def __bool__(self) -> bool:
        return bool(self.tracks)

    def of_kind(self, kind: str) -> tuple[Track, ...]:
        return tuple(track for track in self.tracks if track.kind == kind)

    def music(self) -> tuple[Track, ...]:
        return self.of_kind(MUSIC_KIND)

    def sfx(self) -> tuple[Track, ...]:
        return self.of_kind(SFX_KIND)

    def groups(self, kind: str) -> tuple[str, ...]:
        return tuple(sorted({t.group for t in self.of_kind(kind) if t.group}))

    def moods(self) -> tuple[str, ...]:
        return self.groups(MUSIC_KIND)

    def roles(self) -> tuple[str, ...]:
        return self.groups(SFX_KIND)

    def for_group(self, kind: str, group: str) -> tuple[Track, ...]:
        return tuple(t for t in self.of_kind(kind) if t.group == group)

    def for_mood(self, mood: str) -> tuple[Track, ...]:
        return self.for_group(MUSIC_KIND, mood)

    def for_role(self, role: str) -> tuple[Track, ...]:
        return self.for_group(SFX_KIND, role)

    def unattributed(self) -> tuple[Track, ...]:
        return tuple(track for track in self.tracks if not track.attributed)

    def unreadable(self) -> tuple[Track, ...]:
        return tuple(track for track in self.tracks if track.probe_error)

    def warnings(self) -> tuple[str, ...]:
        """Everything worth telling a human, phrased once so every caller agrees.

        Every line here is advisory. None of them stops a render: an unrecorded
        track still plays, and an unreadable one is skipped by whatever would
        have used it.
        """
        lines: list[str] = []
        if self.record_warning:
            lines.append(self.record_warning)
        missing = self.unattributed()
        if missing:
            names = ", ".join(track.key for track in missing[:3])
            more = f" (+{len(missing) - 3} more)" if len(missing) > 3 else ""
            lines.append(
                f"{len(missing)} file(s) are not recorded in {LIBRARY_FILENAME}, "
                f"so nothing can credit them: {names}{more}"
            )
        if self.orphan_records:
            names = ", ".join(self.orphan_records[:3])
            more = (
                f" (+{len(self.orphan_records) - 3} more)" if len(self.orphan_records) > 3 else ""
            )
            lines.append(
                f"{len(self.orphan_records)} {LIBRARY_FILENAME} entr(ies) name a file that is "
                f"no longer on disk: {names}{more}"
            )
        for track in self.unreadable():
            lines.append(f"{track.key} could not be read by ffprobe: {track.probe_error}")
        return tuple(lines)


# -------------------------------------------------------------------- the index


@dataclass(frozen=True)
class Probed:
    """What the index remembers about one file, and the bytes it measured."""

    size_bytes: int
    mtime_ns: int
    duration_s: float
    probe_error: str = ""

    def matches(self, size_bytes: int, mtime_ns: int) -> bool:
        return self.size_bytes == size_bytes and self.mtime_ns == mtime_ns


def read_index(path: Path) -> dict[str, Probed]:
    """The cached probe results in `path`, or `{}` if there is nothing usable there.

    Every failure mode collapses to `{}` on purpose. This file is a cache: a
    missing, truncated, hand-edited or older-format index must cost a re-probe,
    never an exception on a command the user ran to *look* at their library.
    """
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict) or data.get("version") != INDEX_VERSION:
        return {}
    entries: dict[str, Probed] = {}
    for entry in data.get("tracks") or []:
        try:
            entries[str(entry["key"])] = Probed(
                size_bytes=int(entry["size_bytes"]),
                mtime_ns=int(entry["mtime_ns"]),
                duration_s=float(entry["duration_s"]),
                probe_error=str(entry.get("probe_error", "")),
            )
        except (TypeError, ValueError, KeyError, AttributeError):
            continue  # one unreadable row must not discard the rest
    return entries


def write_index(library: Library, path: Path, *, clock: Callable[[], float] = time.time) -> None:
    """Write `library`'s probe results to `path`, atomically."""
    payload = {
        "version": INDEX_VERSION,
        "generated_at": clock(),
        "tracks": [
            {
                "key": track.key,
                "kind": track.kind,
                "group": track.group,
                "path": str(track.path),
                "size_bytes": track.size_bytes,
                "mtime_ns": track.mtime_ns,
                "duration_s": track.duration_s,
                "probe_error": track.probe_error,
            }
            for track in library.tracks
        ],
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f"{path.name}.tmp"
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, path)


# --------------------------------------------------------------------- the scan


def _audio_files(root: Path) -> list[Path]:
    """Every audio file under `root`, sorted. A `root` that does not exist has none."""
    root = Path(root)
    if not root.is_dir():
        return []
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and not path.name.startswith(".")
        and path.suffix.lower() in AUDIO_SUFFIXES
    )


@dataclass
class _Counts:
    probed: int = 0
    reused: int = 0
    tracks: list[Track] = field(default_factory=list)
    credited: set[str] = field(default_factory=set)


def _scan_kind(
    root: Path,
    kind: str,
    *,
    records: dict[str, Attribution],
    cached: dict[str, Probed],
    probe: Callable[[Path], float] | None,
    counts: _Counts,
) -> None:
    for path in _audio_files(root):
        relative = path.relative_to(root)
        key = f"{kind}/{relative.as_posix()}"
        group = relative.parts[0] if len(relative.parts) > 1 else ""
        stat = path.stat()

        entry = cached.get(key)
        if entry is not None and entry.matches(stat.st_size, stat.st_mtime_ns):
            duration, error = entry.duration_s, entry.probe_error
            counts.reused += 1
        elif probe is None:
            duration, error = 0.0, ""
        else:
            try:
                duration, error = float(probe(path)), ""
            except FFmpegError as exc:
                duration, error = 0.0, str(exc).splitlines()[0]
            counts.probed += 1

        record_key = lookup_record(records, key)
        if record_key:
            counts.credited.add(record_key)
        counts.tracks.append(
            Track(
                key=key,
                kind=kind,
                group=group,
                path=path,
                duration_s=duration,
                size_bytes=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                attribution=records.get(record_key) if record_key else None,
                probe_error=error,
            )
        )


def scan(
    music_dir: Path,
    sfx_dir: Path,
    *,
    index_path: Path | None = None,
    probe: Callable[[Path], float] | None = probe_duration,
) -> Library:
    """Walk the library on disk and describe what is there.

    The filesystem is the only authority on *which* files exist. `index_path`, if
    given, is consulted for durations alone, and only for a file whose size and
    mtime still match the bytes that duration was measured from — so a track
    deleted since the index was written simply does not appear, and one replaced
    since is re-probed. Pass `probe=None` to skip ffprobe entirely (`doctor`
    counts and credits files; it has no business shelling out per track).
    """
    records, record_warning = read_records(Path(music_dir) / LIBRARY_FILENAME)
    cached = read_index(index_path) if index_path is not None else {}
    counts = _Counts()
    for root, kind in ((music_dir, MUSIC_KIND), (sfx_dir, SFX_KIND)):
        _scan_kind(
            Path(root), kind, records=records, cached=cached, probe=probe, counts=counts
        )

    orphans = tuple(sorted(set(records) - counts.credited))
    return Library(
        tracks=tuple(counts.tracks),
        orphan_records=orphans,
        record_warning=record_warning,
        probed=counts.probed,
        reused=counts.reused,
    )


#: Below this, a duration is shown in tenths. SFX are routinely under a second,
#: and `0:00` for a half-second whoosh reads as "broken" rather than "short".
SUB_SECOND_DISPLAY_S = 10.0


def format_duration(seconds: float) -> str:
    """`m:ss` for a track, `0.5s` for an effect, `?` for a file ffprobe could not read."""
    if seconds <= 0:
        return "?"
    if seconds < SUB_SECOND_DISPLAY_S:
        return f"{seconds:.1f}s"
    minutes, remainder = divmod(round(seconds), 60)
    return f"{minutes}:{remainder:02d}"
