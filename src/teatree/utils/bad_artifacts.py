"""Track bad database artifacts (corrupt snapshots/dumps) to skip on future runs.

Stores absolute paths in ``~/.local/share/teatree/bad_artifacts.json``.  The import
engine checks this list before attempting a restore and marks artifacts that
fail restore or migration.
"""

import json

from teatree.config import DATA_DIR

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
    _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_FILE.write_text(json.dumps(sorted(set(paths)), indent=2) + "\n", encoding="utf-8")


def is_bad(path: str) -> bool:
    return path in _read()


def mark_bad(path: str) -> None:
    paths = _read()
    if path not in paths:
        paths.append(path)
        _write(paths)


def unmark(path: str) -> None:
    paths = _read()
    if path in paths:
        paths.remove(path)
        _write(paths)


def list_bad() -> list[str]:
    return _read()


def clear_all() -> None:
    if _CACHE_FILE.is_file():
        _CACHE_FILE.unlink()
