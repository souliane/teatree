"""Resource-pressure mini-loop — disk/RAM auto-free, intake sizing (#3992), artifact eviction (#4244).

All three scanners are time-sensitive: each carries its own internal cadence
plus a stamp on the shared ``ResourcePressureMarker``, and the legacy fan-out
constructed them on every tick. The loop's outer cadence is therefore the
registry floor so the registry gate never throttles below any scanner's own
cadence — matching the legacy per-tick construction rather than the hourly
housekeeping cadence.

The jobs answer different questions off the same box — "is it in trouble",
"how much of it may intake use", and "which rebuildable artifacts have gone
dormant" — and each is independently switchable. The third is deliberately NOT
gated on the pressure bands: reclaiming a build product loses nothing at any
fullness, so a threshold could only ever delay it.
"""

from typing import TYPE_CHECKING

from teatree.loops.base import LoopDeterminism, MiniLoop

if TYPE_CHECKING:
    from teatree.loop.job_identity import _ScannerJob

_REGISTRY_CADENCE_FLOOR = 60


def _build_jobs(**_: object) -> "list[_ScannerJob]":
    from teatree.loop.global_scanner_factories import (  # noqa: PLC0415 — tick-time import
        _artifact_eviction_scanner,
        _intake_concurrency_scanner,
        _resource_pressure_scanner,
    )
    from teatree.loop.job_identity import _ScannerJob  # noqa: PLC0415 — deferred: loaded at tick time, not import

    built = (_resource_pressure_scanner(), _intake_concurrency_scanner(), _artifact_eviction_scanner())
    return [_ScannerJob(scanner=scanner, overlay="") for scanner in built if scanner is not None]


MINI_LOOP = MiniLoop(
    name="resource_pressure",
    default_cadence_seconds=_REGISTRY_CADENCE_FLOOR,
    cadence_is_floor=True,
    build_jobs=_build_jobs,
    declared_reach=frozenset(),
    determinism=LoopDeterminism.DETERMINISTIC,
)
