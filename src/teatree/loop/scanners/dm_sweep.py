"""Scanner that runs the hourly owner-DM hygiene pass (#3658).

Thin: the decision logic is :mod:`teatree.core.owner_dm_sweep`. The pass uses the
``dm_sweep`` loop row's watermark to cover new ground without re-reading history.

It emits a :class:`ScanSignal` ONLY when it actually resolved something. A pass with
nothing to do is silent by design: the loop exists to reduce what the owner reads, so a
"swept, nothing to do" line would be the exact noise it removes.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from django.db import OperationalError, ProgrammingError

from teatree.loop.scanners.base import ScanSignal

if TYPE_CHECKING:
    from teatree.core.backend_protocols import MessagingBackend

logger = logging.getLogger(__name__)

_LOOP_NAME = "dm_sweep"


@dataclass(slots=True)
class DmSweepScanner:
    backend: "MessagingBackend | None" = None
    name: str = "dm_sweep"

    def scan(self) -> list[ScanSignal]:
        from teatree.core.owner_dm_sweep import SweepSeams, run_sweep  # noqa: PLC0415 — deferred: tick-time import

        try:
            result = run_sweep(since=_watermark(), seams=SweepSeams())
        except (OperationalError, ProgrammingError):
            logger.info("%s: tables unavailable (DB not migrated yet) — skipping", self.name)
            return []
        if result.silent:
            return []
        return [
            ScanSignal(
                kind="dm_sweep.resolved",
                summary=f"resolved {result.resolved} owner DM thread(s) that no longer need you",
                payload={"resolved": result.resolved, "left_open": result.left_open},
            ),
        ]


def _watermark() -> datetime | None:
    """The previous pass's start, from the loop's own ledger; ``None`` on the first pass."""
    from teatree.core.models import Loop  # noqa: PLC0415 — deferred: ORM import needs the app registry

    row = Loop.objects.filter(name=_LOOP_NAME).first()
    return row.last_run_at if row is not None else None


__all__ = ["DmSweepScanner"]
