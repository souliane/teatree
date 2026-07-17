"""Migrated-DB template restore helper (W7-PR2).

The stock ``django_db_setup`` fixture re-runs a full ``migrate`` (schema DDL +
the squashed ``0001_initial`` seed ``RunPython``) in EVERY xdist worker process
that needs the DB — the dominant per-worker cost of a migrations-backed sqlite
``:memory:`` suite. This module snapshots the FIRST worker's freshly-migrated
in-memory DB to an on-disk template file (:func:`publish_from_connection`) so
every later worker in the same checkout restores that exact byte-for-byte state
via :func:`restore_into_connection` instead of re-running ``migrate``.

The template is content-addressed by :func:`schema_hash` — the sqlite version
plus every migration file's bytes plus ``uv.lock`` plus
``tests/django_settings.py`` — so a stale template can never be silently
reused: any input that could change what a fresh migrate produces changes the
digest and forces a rebuild. :func:`template_build_lock` serializes concurrent
builders (multiple ``-n auto`` xdist workers inside one CI shard container
share one bind-mounted checkout) the same way
``src/teatree/paths.py::_exclusive_lock`` serializes the canonical-DB seed.
"""

import fcntl
import hashlib
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = _REPO_ROOT / ".pytest-db-template"


def schema_hash(repo_root: Path = _REPO_ROOT) -> str:
    """Fingerprint every input that shapes a fresh ``migrate`` on *repo_root*."""
    h = hashlib.sha256()
    h.update(sqlite3.sqlite_version.encode())
    inputs = sorted(repo_root.glob("src/**/migrations/*.py"))
    inputs += [repo_root / "uv.lock", repo_root / "tests" / "django_settings.py"]
    for path in inputs:
        h.update(str(path.relative_to(repo_root)).encode())
        h.update(path.read_bytes())
    return h.hexdigest()[:16]


def template_path(digest: str, *, template_dir: Path = TEMPLATE_DIR) -> Path:
    """The on-disk template file for a given :func:`schema_hash` digest."""
    return template_dir / f"template-{digest}.sqlite3"


def publish_from_connection(source: sqlite3.Connection, dest: Path) -> None:
    """Snapshot *source* to *dest* atomically, then prune stale sibling templates.

    Writes to a pid-suffixed temp sibling first and publishes with a
    same-filesystem :meth:`Path.replace` so a concurrent restorer never
    observes a partially-written template.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dest.name}.{os.getpid()}.", suffix=".tmp", dir=dest.parent)
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        snapshot = sqlite3.connect(tmp_path)
        try:
            source.backup(snapshot)
        finally:
            snapshot.close()
        tmp_path.replace(dest)
    finally:
        tmp_path.unlink(missing_ok=True)
    for stale in dest.parent.glob("template-*.sqlite3"):
        if stale != dest:
            stale.unlink(missing_ok=True)


def restore_into_connection(template: Path, dest: sqlite3.Connection) -> None:
    """Overwrite every page of *dest* with an exact copy of the on-disk *template*.

    ``?immutable=1`` (not ``?mode=ro``) opens the source without needing a
    ``-shm``/``-wal`` sidecar — mirrors ``src/teatree/paths.py::_sqlite_snapshot``.
    """
    source = sqlite3.connect(f"file:{template}?immutable=1", uri=True)
    try:
        source.backup(dest)
    finally:
        source.close()


@contextmanager
def template_build_lock(lock_dir: Path = TEMPLATE_DIR) -> Iterator[None]:
    """Serialize template (re)builds across concurrent xdist workers sharing one checkout."""
    lock_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_dir / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
