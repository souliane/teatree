"""Dormant-artifact eviction as its own job, off the disk-CRIT band (#4244).

Reclaiming a rebuildable build product loses nothing at any fullness, so there is
no threshold worth having and nothing to gate the pass on but its own cadence. The
alternative — hanging it off ``resource.cleanup_needed`` — would drag the whole
destructive ladder along with it (``allow_destructive_disk``, the RAM kill list, the
scratch sweep) and inherit that ladder's anti-thrash debounce, which downgrades a
throttled CRITICAL to a WARN that frees nothing.

The cadence field is :attr:`ResourcePressureMarker.last_artifact_sweep_at`, written
by the mechanical action once a pass actually runs and read only here. Keeping it
distinct from ``last_freed_at`` is what stops a loss-free sweep from rate-limiting
the destructive ladder; keeping the action as its sole writer is what makes a signal
nothing acted on re-fire rather than silently consume its window.
"""

import logging
from dataclasses import dataclass

from django.utils import timezone

from teatree.loop.scanners.base import ScanSignal

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ArtifactEvictionScanner:
    """Invite a dormant-artifact sweep once per ``cadence_minutes`` — never threshold-gated.

    Never threshold-gated is not the same as pressure-blind: the disk thresholds ride the
    payload so the sweep can DECAY its retention window (#4644), which only ever widens the
    candidate set. Nothing here decides whether the pass runs.
    """

    artifact_idle_days: float = 2.0
    disk_warn_free_gb: float = 25.0
    disk_crit_free_gb: float = 10.0
    cadence_minutes: int = 30
    name: str = "artifact_eviction"

    def scan(self) -> list[ScanSignal]:
        from teatree.core.models.resource_pressure_marker import (  # noqa: PLC0415 — ORM loads at tick time (#4244)
            ResourcePressureMarker,
        )

        try:
            marker = ResourcePressureMarker.load()
        except Exception:
            logger.exception("artifact_eviction: could not load marker — skipping tick")
            return []
        if self._cadence_blocks(marker):
            return []
        return [
            ScanSignal(
                kind="resource.artifacts_reclaimable",
                summary=f"sweeping checkouts for artifacts idle over {self.artifact_idle_days:.1f}d",
                payload={
                    "artifact_idle_days": self.artifact_idle_days,
                    # The sweep decays that window by how far below these the box actually
                    # is (#4644). The reading is the pressure scanner's own, at most one of
                    # its 5-minute cadences old; a marker that has none yet yields no
                    # decay, so an unmeasured box gets the full window rather than a guess.
                    "free_gb": getattr(marker, "last_disk_free_gb", None),
                    "disk_warn_free_gb": self.disk_warn_free_gb,
                    "disk_crit_free_gb": self.disk_crit_free_gb,
                },
            )
        ]

    def _cadence_blocks(self, marker: object) -> bool:
        last_sweep = getattr(marker, "last_artifact_sweep_at", None)
        if last_sweep is None:
            return False
        return (timezone.now() - last_sweep).total_seconds() / 60.0 < self.cadence_minutes


__all__ = ["ArtifactEvictionScanner"]
