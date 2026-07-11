"""DB-backup mini-loop — daily control-DB backup cadence anchor (directive #2).

The scanner (:mod:`teatree.loop.scanners.db_backup`) is cadence-gated on the
newest artifact's OWN embedded timestamp, so the loop's outer cadence is only a
daily floor: there is no value checking more often than the ``db_backup_cadence_hours``
window (default 24h). The scanner FLAGS a due backup; the ``run_db_backup``
mechanical handler does the actual snapshot + retention prune off the tick.
"""

from typing import TYPE_CHECKING

from teatree.loops.base import MiniLoop

if TYPE_CHECKING:
    from teatree.loop.job_identity import _ScannerJob

_REGISTRY_CADENCE_FLOOR = 86400


def _build_jobs(**_: object) -> "list[_ScannerJob]":
    from teatree.loop.global_scanner_factories import _db_backup_scanner  # noqa: PLC0415 deferred: cycle-break
    from teatree.loop.job_identity import _ScannerJob  # noqa: PLC0415 deferred: needed only at fan-out

    scanner = _db_backup_scanner()
    if scanner is None:
        return []
    return [_ScannerJob(scanner=scanner, overlay="")]


MINI_LOOP = MiniLoop(
    name="db_backup",
    default_cadence_seconds=_REGISTRY_CADENCE_FLOOR,
    build_jobs=_build_jobs,
)
