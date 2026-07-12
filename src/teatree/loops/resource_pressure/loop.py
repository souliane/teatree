"""Resource-pressure mini-loop — host disk/RAM auto-free (#128).

The scanner is time-sensitive: it carries its own ~5-minute internal
cadence (``ResourcePressureScanner.cadence_minutes``) plus a
``ResourcePressureMarker``, and the legacy fan-out constructed it on
every tick. The loop's outer cadence is therefore the registry floor so
the registry gate never throttles below the scanner's own cadence —
matching the legacy per-tick construction rather than the hourly
housekeeping cadence.
"""

from typing import TYPE_CHECKING

from teatree.loops.base import MiniLoop

if TYPE_CHECKING:
    from teatree.loop.job_identity import _ScannerJob

_REGISTRY_CADENCE_FLOOR = 60


def _build_jobs(**_: object) -> "list[_ScannerJob]":
    from teatree.loop.global_scanner_factories import _resource_pressure_scanner  # noqa: PLC0415 — tick-time import
    from teatree.loop.job_identity import _ScannerJob  # noqa: PLC0415 — deferred: loaded at tick time, not import

    scanner = _resource_pressure_scanner()
    if scanner is None:
        return []
    return [_ScannerJob(scanner=scanner, overlay="")]


MINI_LOOP = MiniLoop(
    name="resource_pressure",
    default_cadence_seconds=_REGISTRY_CADENCE_FLOOR,
    build_jobs=_build_jobs,
)
