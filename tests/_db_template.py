"""Cross-process migrated-SQLite-template sharing for pytest-xdist workers.

Each xdist worker process pays a full Django migrate the first time it
touches the test database. With N workers per test session that is N
redundant runs of the identical migration graph against the same
``:memory:`` sqlite target. These primitives let exactly one worker build a
migrated template file (guarded by a cross-process file lock so concurrent
workers never race the build), and every worker — including the builder —
restore its own private connection from that template via
``sqlite3.Connection.backup()`` instead of re-running migrations. Used by the
``django_db_setup`` override in ``tests/conftest.py``.
"""

import fcntl
import sqlite3
from collections.abc import Callable
from pathlib import Path


def build_or_reuse_template(template_path: Path, lock_path: Path, build: Callable[[Path], None]) -> None:
    """Build ``template_path`` via ``build`` unless it already exists.

    Safe under concurrent callers (one per xdist worker process): an
    exclusive ``flock`` on ``lock_path`` serializes access, and the
    existence check happens *inside* the lock so only the first caller to
    arrive ever invokes ``build``. ``build`` receives a private
    ``<name>.building`` path and must write a complete database there; the
    file is renamed into place only after ``build`` returns, so a crash or
    exception mid-build never leaves a partially-migrated file sitting at
    ``template_path`` for a later caller to reuse.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            if template_path.exists():
                return
            building_path = template_path.parent / f"{template_path.name}.building"
            building_path.unlink(missing_ok=True)
            build(building_path)
            building_path.rename(template_path)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def restore_from_template(template_path: Path, target: sqlite3.Connection) -> None:
    """Copy ``template_path``'s contents into the already-open ``target`` connection."""
    source = sqlite3.connect(str(template_path))
    try:
        source.backup(target)
    finally:
        source.close()
