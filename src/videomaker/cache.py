import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

STAGE_ORDER: tuple[str, ...] = (
    "script",
    "voice",
    "align",
    "visuals",
    "captions",
    "assemble",
    "render",
    # Last, deliberately: `derive_status` returns the status of the last *current*
    # stage, so appending here cannot lower any project's status. See
    # `pipeline/thumbnail.py` for why that is what makes its unit `required`.
    "thumbnail",
)

HASH_LEN = 16
FLOAT_PLACES = 6
SECONDS_PER_DAY = 86400


def stage_key(stage: str, unit: str = "all") -> str:
    return f"{stage}:{unit}"


def _canonical(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, FLOAT_PLACES)
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in sorted(value.items())}
    if isinstance(value, list | tuple):
        return [_canonical(v) for v in value]
    return value


def hash_inputs(**parts: object) -> str:
    blob = json.dumps(_canonical(parts), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:HASH_LEN]


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f"{path.name}.tmp"
    tmp.write_text(text)
    os.replace(tmp, path)


class StageCache:
    """Maps `stage:unit` keys to the input hash that last produced their output."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._hashes: dict[str, str] = self._read()

    def _read(self) -> dict[str, str]:
        try:
            data = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items()}

    def is_stale(self, key: str, current_hash: str) -> bool:
        return self._hashes.get(key) != current_hash

    def mark(self, key: str, current_hash: str) -> None:
        self._hashes[key] = current_hash

    def invalidate(self, prefix: str) -> None:
        for key in [k for k in self._hashes if k.startswith(prefix)]:
            del self._hashes[key]

    def save(self) -> None:
        _write_atomic(self.path, json.dumps(self._hashes, indent=2, sort_keys=True))


class ResponseCache:
    """Content-addressed provider-response cache; `ttl_days=None` never expires."""

    def __init__(self, root: Path, ttl_days: int | None = None) -> None:
        self.root = Path(root)
        self.ttl_days = ttl_days

    def path_for(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def _expired(self, stored_at: float) -> bool:
        if self.ttl_days is None:
            return False
        return time.time() - stored_at > self.ttl_days * SECONDS_PER_DAY

    def get(self, key: str) -> dict | None:
        try:
            entry = json.loads(self.path_for(key).read_text())
        except (json.JSONDecodeError, OSError):
            return None
        if not isinstance(entry, dict) or not isinstance(entry.get("value"), dict):
            return None
        if self._expired(float(entry.get("stored_at", 0.0))):
            return None
        return entry["value"]

    def put(self, key: str, value: dict) -> None:
        entry = {"stored_at": time.time(), "value": value}
        _write_atomic(self.path_for(key), json.dumps(entry, separators=(",", ":")))
