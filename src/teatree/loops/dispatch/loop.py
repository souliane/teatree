"""Dispatch mini-loop definition — always-on global scanners."""

from typing import Any

from teatree.loops.base import MiniLoop


def _build_jobs(**_: Any) -> list[Any]:  # noqa: ANN401 — orchestrator passes extra context as open kwargs
    """Build the always-on global scanner jobs.

    Delegates to :mod:`teatree.loop.tick_jobs` so the legacy fan-out stays
    the single source of truth for which scanners run in this mini-loop.
    The orchestrator passes its per-tick kwargs through; the legacy
    builder consumes whichever it understands.
    """
    from teatree.loop.scanners import IncomingEventsScanner, OutboundAuditScanner, PendingTasksScanner  # noqa: PLC0415
    from teatree.loop.tick_jobs import _ScannerJob  # noqa: PLC0415

    return [
        _ScannerJob(scanner=PendingTasksScanner(), overlay=""),
        _ScannerJob(scanner=IncomingEventsScanner(), overlay=""),
        _ScannerJob(scanner=OutboundAuditScanner(), overlay=""),
    ]


MINI_LOOP = MiniLoop(
    name="dispatch",
    default_cadence_seconds=300,
    build_jobs=_build_jobs,
    always_on=True,
)
