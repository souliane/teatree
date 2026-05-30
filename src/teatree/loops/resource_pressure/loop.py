"""Resource-pressure mini-loop — host disk/RAM auto-free (#128).

The scanner is time-sensitive: it carries its own ~5-minute internal
cadence (``ResourcePressureScanner.cadence_minutes``) plus a
``ResourcePressureMarker``, and the legacy fan-out constructed it on
every tick. The loop's outer cadence is therefore the registry floor so
the registry gate never throttles below the scanner's own cadence —
matching the legacy per-tick construction rather than the hourly
housekeeping cadence.
"""

from typing import Any

from teatree.loops.base import MiniLoop

_REGISTRY_CADENCE_FLOOR = 60


def _build_jobs(**_: Any) -> list[Any]:  # noqa: ANN401 — orchestrator passes extra context as open kwargs
    from teatree.loop.tick_jobs import _resource_pressure_scanner, _ScannerJob  # noqa: PLC0415

    scanner = _resource_pressure_scanner()
    if scanner is None:
        return []
    return [_ScannerJob(scanner=scanner, overlay="")]


MINI_LOOP = MiniLoop(
    name="resource_pressure",
    default_cadence_seconds=_REGISTRY_CADENCE_FLOOR,
    build_jobs=_build_jobs,
)
