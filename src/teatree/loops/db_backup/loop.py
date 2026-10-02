"""DB-backup mini-loop — daily control-DB backup cadence anchor (directive #2).

This row IS the cadence — a 02:00 wall-clock anchor, so the backup cannot drift later
each day the way a daily tick chained to a 24h-since-last-backup gate did. The scanner
FLAGS a due backup; the ``run_db_backup`` mechanical handler does the actual snapshot +
retention prune off the tick.
"""

from typing import TYPE_CHECKING

from teatree.loops.base import LoopDeterminism, MiniLoop

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
    cadence_is_floor=True,
    build_jobs=_build_jobs,
    declared_reach=frozenset(),
    determinism=LoopDeterminism.DETERMINISTIC,
)
