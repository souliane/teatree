"""Snapshot-warmer mini-loop — keeps reference-DB DSLR snapshots current out-of-band (souliane/teatree#2949).

The scanner is time-sensitive in the same sense as the idle-stack reaper: a
snapshot's OWN embedded date doubles as its "last refreshed" timestamp, so no
separate cadence marker is needed — once refreshed today, the scan finds it
fresh and stops emitting. The loop's outer cadence is a daily floor: a
reference-DB refresh is a slow (restore+migrate) operation, so there is no
value checking more often than once a day.
"""

from typing import TYPE_CHECKING

from teatree.loops.base import MiniLoop

if TYPE_CHECKING:
    from teatree.loop.job_identity import _ScannerJob

_REGISTRY_CADENCE_FLOOR = 86400


def _build_jobs(**_: object) -> "list[_ScannerJob]":
    from teatree.loop.global_scanner_factories import _snapshot_warmer_scanner  # noqa: PLC0415 — tick-time import
    from teatree.loop.job_identity import _ScannerJob  # noqa: PLC0415 — deferred: loaded at tick time, not import

    scanner = _snapshot_warmer_scanner()
    if scanner is None:
        return []
    return [_ScannerJob(scanner=scanner, overlay="")]


MINI_LOOP = MiniLoop(
    name="snapshot_warmer",
    default_cadence_seconds=_REGISTRY_CADENCE_FLOOR,
    build_jobs=_build_jobs,
)
