"""Track bad database artifacts (corrupt snapshots/dumps) to skip on future runs.

Stores absolute paths in ``~/.local/share/teatree/bad_artifacts.json``.  The import
engine checks this list before attempting a restore and marks artifacts that
fail restore or migration.
"""

import contextlib
import fcntl
import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

from teatree.paths import DATA_DIR

_CACHE_FILE = DATA_DIR / "bad_artifacts.json"


def _read() -> list[str]:
    if not _CACHE_FILE.is_file():
        return []
    try:
        data = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def _write(paths: list[str]) -> None:
    """Publish the list atomically, so an interrupted write never truncates the ledger."""
    _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".bad-artifacts-", dir=_CACHE_FILE.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(sorted(set(paths)), indent=2) + "\n")
        tmp_path.replace(_CACHE_FILE)
    finally:
        tmp_path.unlink(missing_ok=True)


@contextlib.contextmanager
def _exclusive() -> Iterator[None]:
    """Serialize the read-modify-write across processes — an unlocked pair loses marks."""
    _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _CACHE_FILE.with_suffix(".lock").open("a", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)


def is_bad(path: str) -> bool:
    return path in _read()


def mark_bad(path: str) -> None:
    with _exclusive():
        paths = _read()
        if path not in paths:
            paths.append(path)
            _write(paths)


def unmark(path: str) -> None:
    with _exclusive():
        paths = _read()
        if path in paths:
            paths.remove(path)
            _write(paths)


def list_bad() -> list[str]:
    return _read()


def clear_all() -> None:
    if _CACHE_FILE.is_file():
        _CACHE_FILE.unlink()
