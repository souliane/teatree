"""Return a pruned control DB's free pages to the filesystem (#3852).

The half a retention prune cannot do. SQLite never shrinks a file on DELETE: the
freed pages go onto the free list and are reused by later inserts, so the row
count drops and the FILE stays exactly as large. ``PRAGMA auto_vacuum`` is ``0``
on the control DB and cannot be turned on in place — it is a create-time property
short of a full rebuild — so without an explicit ``VACUUM`` the free list only
ever grows.

That is not a tidiness concern here. The control DB is the seed every
auto-isolated worktree env dir is copied from (:func:`teatree.paths.seed_isolated_db`),
so its size is multiplied by the number of live checkouts: measured before this
landed, 147,790 of 292,627 pages free — half of a 1.12 GB file — copied into each
of 169 env dirs.

``VACUUM`` rebuilds the database into a fresh file and therefore cannot run inside
a transaction; it must follow the prune's commit rather than join it. It also
needs transient free space of roughly the database's own size while the rebuild
is in flight.

**Concurrency is left to SQLite's own exclusive lock — a deliberate choice, not an
oversight.** A second concurrent vacuum loses the race and surfaces as
``sqlite3.Error`` → ``ran=False`` with the reason, which is a correct outcome:
``VACUUM`` publishes the rebuilt database atomically, so a losing run leaves the
file untouched rather than half-written, and no half-state is reachable. An
application-level lock would add a failure mode (a stale lock wedging maintenance
on a full disk) to prevent an outcome that is already safe.
"""

import logging
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from django.db import DEFAULT_DB_ALIAS, connections

logger = logging.getLogger(__name__)

_IN_MEMORY_NAMES = frozenset({":memory:", ""})
#: SQLite's shared-cache in-memory URI form, e.g.
#: ``file:memorydb_default?mode=memory&cache=shared``. Django switches an
#: in-memory test database to this once a ``TransactionTestCase`` needs the DB
#: reachable from more than one connection, so a name-equality check alone sees
#: it as an ordinary (missing) file — which is why matching only ``:memory:``
#: read as "no such file" depending on what had run before.
_IN_MEMORY_URI_MARKER = "mode=memory"


def _is_in_memory(name: str) -> bool:
    return name in _IN_MEMORY_NAMES or _IN_MEMORY_URI_MARKER in name


#: VACUUM rebuilds into a full second copy before swapping, so the transient peak
#: is the database's own size again, plus rollback-journal slack. Measured need on
#: a 1.2 GB control DB is ~1.2 GB; the 10% margin covers the journal.
_REBUILD_HEADROOM_FACTOR = 1.1


def _required_headroom_bytes(db_size: int) -> int:
    return int(db_size * _REBUILD_HEADROOM_FACTOR)


@dataclass(frozen=True, slots=True)
class VacuumOutcome:
    """What the vacuum did, or the stated reason it did nothing.

    ``ran=False`` is always accompanied by a ``reason``: a caller reporting "0
    bytes reclaimed" must be able to tell a vacuum that found nothing to reclaim
    from one that never happened.
    """

    ran: bool
    reason: str
    bytes_before: int = 0
    bytes_after: int = 0

    @property
    def bytes_reclaimed(self) -> int:
        return max(self.bytes_before - self.bytes_after, 0)


def vacuum_sqlite(path: Path) -> VacuumOutcome:
    """Rebuild the SQLite file at *path*, returning its free pages to the filesystem.

    Never raises: an in-memory database (the test suite's own), an absent file, or
    a locked/corrupt one all report ``ran=False`` with the reason. A maintenance
    step that aborts the command it was appended to would be worse than one that
    reclaims nothing.
    """
    if _is_in_memory(str(path)):
        return VacuumOutcome(ran=False, reason=f"{path} is an in-memory database — nothing on disk to reclaim")
    if not path.is_file():
        return VacuumOutcome(ran=False, reason=f"no such file: {path}")
    before = path.stat().st_size
    # Checked BEFORE opening: a rebuild that runs out of disk part-way through has
    # already written most of a second copy onto a filesystem that was short to
    # begin with — on a box already reclaiming space, the worst possible moment to
    # add a gigabyte. Refusing up front leaves the file untouched.
    free = shutil.disk_usage(path.parent).free
    required = _required_headroom_bytes(before)
    if free < required:
        return VacuumOutcome(
            ran=False,
            reason=(
                f"insufficient rebuild headroom: VACUUM needs ~{required / 1024**2:.0f} MiB free "
                f"(a full second copy of the {before / 1024**2:.0f} MiB database) but only "
                f"{free / 1024**2:.0f} MiB is available on {path.parent} — reclaim space first, "
                "e.g. `t3 <overlay> workspace reclaim-disk`"
            ),
            bytes_before=before,
            bytes_after=before,
        )
    try:
        connection = sqlite3.connect(path, isolation_level=None)  # autocommit: VACUUM cannot run in a transaction
        try:
            connection.execute("VACUUM")
        finally:
            connection.close()
    except sqlite3.Error as exc:
        logger.warning("db_vacuum: VACUUM on %s failed: %s", path, exc)
        return VacuumOutcome(ran=False, reason=f"VACUUM failed: {exc}", bytes_before=before, bytes_after=before)
    return VacuumOutcome(ran=True, reason="rebuilt", bytes_before=before, bytes_after=path.stat().st_size)


def vacuum_control_db(*, using: str = DEFAULT_DB_ALIAS) -> VacuumOutcome:
    """Vacuum the control DB behind the *using* connection.

    Only SQLite is rebuilt this way — another vendor reports ``ran=False`` rather
    than having a SQLite-shaped maintenance step applied to it.

    The connection is closed ONLY for a real on-disk database, so the rebuild
    cannot contend with an idle one holding the file open (Django reopens lazily
    on the next query). Closing unconditionally would destroy an ``:memory:``
    database, which does not survive its connection and has nothing on disk to
    reclaim anyway — a maintenance no-op must not delete the data it declined to
    act on.
    """
    connection = connections[using]
    if connection.vendor != "sqlite":
        return VacuumOutcome(ran=False, reason=f"{connection.vendor} is not SQLite — VACUUM is not applicable")
    path = Path(str(connection.settings_dict["NAME"]))
    if path.is_file():
        connection.close()
    return vacuum_sqlite(path)


__all__ = ["VacuumOutcome", "vacuum_control_db", "vacuum_sqlite"]
