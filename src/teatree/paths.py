"""XDG-compliant data paths — leaf module with no teatree dependencies."""

import os
from collections.abc import Iterator
from pathlib import Path

DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "teatree"
CANONICAL_DB = DATA_DIR / "db.sqlite3"


def get_data_dir(namespace: str) -> Path:
    data_dir = DATA_DIR / namespace
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def find_stale_dbs(data_dir: Path, *, canonical: Path) -> Iterator[Path]:
    """Yield ``db.sqlite3`` files inside ``data_dir`` that aren't ``canonical``.

    Walks recursively under ``data_dir`` so any legacy namespaced layout
    (``data_dir/<name>/db.sqlite3``) surfaces. The canonical path is skipped.
    Used by both the settings warning and the ``t3 doctor`` check.
    """
    if not data_dir.is_dir():
        return
    canonical = canonical.resolve()
    for candidate in data_dir.glob("**/db.sqlite3"):
        if candidate.resolve() == canonical:
            continue
        yield candidate
