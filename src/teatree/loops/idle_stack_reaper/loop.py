"""Idle-stack reaper mini-loop — stop idle stacks to free a slot (#2190).

The scanner is time-sensitive: it carries its own internal cadence
(``IdleStackReaperScanner.cadence_minutes`` + ``LocalStackReaperMarker``) and
the legacy fan-out constructed it on every tick. The loop's outer cadence is
therefore the registry floor so the registry gate never throttles below the
scanner's own cadence (mirrors the resource-pressure mini-loop).
"""

from typing import TYPE_CHECKING

from teatree.loops.base import MiniLoop

if TYPE_CHECKING:
    from teatree.loop.job_identity import _ScannerJob

_REGISTRY_CADENCE_FLOOR = 60


def _build_jobs(**_: object) -> "list[_ScannerJob]":
    from teatree.loop.global_scanner_factories import _idle_stack_reaper_scanner  # noqa: PLC0415 — tick-time import
    from teatree.loop.job_identity import _ScannerJob  # noqa: PLC0415 — deferred: loaded at tick time, not import

    scanner = _idle_stack_reaper_scanner()
    if scanner is None:
        return []
    return [_ScannerJob(scanner=scanner, overlay="")]


MINI_LOOP = MiniLoop(
    name="idle_stack_reaper",
    default_cadence_seconds=_REGISTRY_CADENCE_FLOOR,
    cadence_is_floor=True,
    build_jobs=_build_jobs,
)
