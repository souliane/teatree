"""Track bad database artifacts (corrupt snapshots/dumps) to skip on future runs.

Stores absolute paths in ``~/.local/share/teatree/bad_artifacts.json``.  The import
engine checks this list before attempting a restore and marks artifacts that
fail restore or migration.
"""

import json
from pathlib import Path

from teetree.config import DATA_DIR

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


class BadArtifactCleaner:
    def __init__(self, data_dir: str = "") -> None:
        self.bad_file = Path(data_dir) / "bad_artifacts.json" if data_dir else _CACHE_FILE

    def list_bad_artifacts(self) -> list[str]:
        if not self.bad_file.is_file():
            return []
        try:
            data = json.loads(self.bad_file.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    def remove_artifact(self, path: str) -> bool:
        artifacts = self.list_bad_artifacts()
        if path not in artifacts:
            return False
        artifacts.remove(path)
        target = Path(path)
        if target.is_file():
            target.unlink()
        self.bad_file.write_text(json.dumps(sorted(set(artifacts)), indent=2) + "\n", encoding="utf-8")
        return True
