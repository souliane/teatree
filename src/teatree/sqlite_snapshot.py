"""Consistent point-in-time SQLite copies — leaf module with no teatree dependencies.

Split out of :mod:`teatree.paths`, which seeds each auto-isolated worktree DB
through :func:`_sqlite_snapshot` but is otherwise about resolving paths. The two
concerns share no state, and only this one needs the WAL semantics documented
below.
"""

import fcntl
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def _exclusive_lock(lock_path: Path) -> Iterator[None]:
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


#: The two ``OperationalError`` texts a WAL source whose ``-shm`` cannot be created
#: raises — one at open, one at the first statement that needs the WAL index.
_NO_SHARED_MEMORY_SIGNALS = ("unable to open database file", "readonly database")


def _backup_into(source: sqlite3.Connection, dst: Path) -> None:
    dest = sqlite3.connect(dst)
    try:
        source.backup(dest)
    finally:
        dest.close()


def _sqlite_snapshot(src: Path, dst: Path) -> None:
    """Consistent point-in-time copy INCLUDING commits still living in the ``-wal``.

    ``?mode=ro`` is tried first because it is the case that matters. A live
    WAL-mode DB keeps every commit in its ``-wal`` until a checkpoint folds it
    back into the main file, and only a connection that READS the WAL sees
    them. ``?immutable=1`` does not: it promises SQLite the file cannot change,
    so SQLite skips locking *and* ignores the ``-wal`` entirely. Snapshotting a
    live DB that way silently drops every transaction since the last checkpoint
    and can tear pages under a concurrent writer — which is how an
    unrestorable "backup" gets produced. ``mode=ro`` also takes real read
    locks, so the snapshot is a consistent point in time rather than a smear.

    ``immutable=1`` stays as the FALLBACK, for the case it was actually added
    for: a cold artifact whose ``-shm`` is absent and cannot be created (a
    read-only file or directory). A cold artifact has no uncheckpointed WAL to
    lose, so the fallback is lossless exactly where it applies.

    What decides the fallback is the SNAPSHOT failing, not a probe: which
    statement first needs the WAL index is a SQLite-build detail. A probe
    ``SELECT`` forces the open on some builds and is served without the
    ``-shm`` on others, where the same source then fails at ``backup`` with
    ``attempt to write a readonly database`` — past the guard, so the fallback
    never fired. Both no-``-shm`` signals are caught here and nothing else is:
    any other ``OperationalError`` propagates rather than being retried into a
    WAL-dropping ``immutable=1`` copy.

    The source is never opened read-write, so this stays legal on a host whose
    control DB the containerized stack owns (:mod:`teatree.db.boundary`).
    """
    # ``as_uri`` percent-encodes a path holding a URI-special character (space,
    # ``%``, ``?``, ``#``) instead of malforming the URI into a different open.
    base_uri = src.absolute().as_uri()
    source = sqlite3.connect(f"{base_uri}?mode=ro", uri=True)
    try:
        source.execute("SELECT 1").fetchone()
        _backup_into(source, dst)
    except sqlite3.OperationalError as exc:
        if not any(signal in str(exc) for signal in _NO_SHARED_MEMORY_SIGNALS):
            raise
    else:
        return
    finally:
        source.close()

    source = sqlite3.connect(f"{base_uri}?immutable=1", uri=True)
    try:
        _backup_into(source, dst)
    finally:
        source.close()
