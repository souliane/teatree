"""Local-stack acquisition-queue drainer mini-loop (#2190, #44).

The drainer is time-sensitive: each due queue item carries its own
Fibonacci-minute backoff on ``LocalStackQueueItem.next_attempt_at``, and the
legacy fan-out constructed the scanner on every tick. The loop's outer cadence
is therefore the registry floor so the registry gate never throttles below the
per-item backoff granularity (mirrors the resource-pressure mini-loop).
"""

from typing import TYPE_CHECKING

from teatree.loops.base import MiniLoop

if TYPE_CHECKING:
    from teatree.loop.job_identity import _ScannerJob

_REGISTRY_CADENCE_FLOOR = 60


def _build_jobs(**_: object) -> "list[_ScannerJob]":
    from teatree.loop.global_scanner_factories import (  # noqa: PLC0415 — deferred: loaded at tick time, not import
        _local_stack_queue_drainer_scanner,
    )
    from teatree.loop.job_identity import _ScannerJob  # noqa: PLC0415 — deferred: loaded at tick time, not import

    scanner = _local_stack_queue_drainer_scanner()
    if scanner is None:
        return []
    return [_ScannerJob(scanner=scanner, overlay="")]


MINI_LOOP = MiniLoop(
    name="local_stack_queue",
    default_cadence_seconds=_REGISTRY_CADENCE_FLOOR,
    build_jobs=_build_jobs,
)
