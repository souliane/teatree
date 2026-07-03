"""Snapshot-warmer scanner — keeps each configured reference DB's DSLR snapshot current (souliane/teatree#2949).

Root cause #1 of the ~2-hour parallel-workspace provisioning problem: nothing
keeps the reference DB and its snapshot warm out-of-band, so a ticket
provision either fails outright or pays the slow restore+migrate path itself.
This scanner runs on its own mini-loop cadence, checking every overlay-
declared :class:`~teatree.utils.django_db.DjangoDbImportConfig` for staleness
(:func:`teatree.utils.django_db_snapshot_warmer.snapshot_is_stale`) and
emitting one signal per stale config. No separate cadence marker is needed:
the DSLR snapshot's OWN embedded date is the "last refreshed" timestamp, so
once refreshed today the scan naturally finds it fresh and stops emitting.

The scanner only FLAGS staleness; the mechanical handler
(:mod:`teatree.loop.mechanical_snapshot_warmer`) does the actual (slow)
restore+migrate+snapshot work, mirroring the detect/execute split every other
mechanical scanner uses.
"""

import logging
from dataclasses import dataclass, field

from teatree.loop.scanners.base import ScanSignal
from teatree.utils.django_db import DjangoDbImportConfig
from teatree.utils.django_db_snapshot_warmer import snapshot_is_stale

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SnapshotWarmerScanner:
    """Emit ``snapshot_warmer.refresh_needed`` for each stale configured reference DB."""

    configs: list[DjangoDbImportConfig] = field(default_factory=list)
    max_age_days: int = 1
    name: str = "snapshot_warmer"

    def scan(self) -> list[ScanSignal]:
        signals: list[ScanSignal] = []
        for cfg in self.configs:
            try:
                stale = snapshot_is_stale(cfg, max_age_days=self.max_age_days)
            except Exception:
                logger.exception("snapshot_warmer: staleness check failed for %r — skipping", cfg.ref_db_name)
                continue
            if not stale:
                continue
            signals.append(
                ScanSignal(
                    kind="snapshot_warmer.refresh_needed",
                    summary=f"reference DB {cfg.ref_db_name} snapshot is stale — refreshing out-of-band",
                    payload={"config": cfg},
                )
            )
        return signals


__all__ = ["SnapshotWarmerScanner"]
