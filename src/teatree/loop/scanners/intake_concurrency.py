"""The resource loop's second job — set intake concurrency from observed headroom (#3992).

Deliberately NOT folded into :class:`~teatree.loop.scanners.resource_pressure.ResourcePressureScanner`:
that one answers "is the box in trouble", this one answers "how much of the box may
intake use", and keeping them apart is what makes this removable on its own. It shares
the resource loop's measurement cadence so the ramp advances once per window rather than
once per tick.

The arithmetic lives in :mod:`teatree.core.intake.concurrency`; this scanner only
supplies the signals (cgroup-aware RAM headroom, the live in-flight count, the core
count), persists the answer on the resource loop's own ledger, and emits when the number
MOVES — an adjustment nobody can see is indistinguishable from a knob nobody turned.
"""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.utils import timezone

from teatree.core.intake.budget import read_intake_budget
from teatree.core.intake.concurrency import BoxSizing, adapt_concurrency
from teatree.loop.scanners.base import ScanSignal
from teatree.utils.ram_probe import available_cpu_count, effective_available_ram_mib

if TYPE_CHECKING:
    from teatree.core.models.resource_pressure_marker import ResourcePressureMarker

logger = logging.getLogger(__name__)

_MIB_PER_GB = 1024.0


@dataclass(frozen=True, slots=True)
class _Decision:
    """One window's answer, plus the reading and the value it moved from."""

    concurrency: int
    previous: int
    available_gb: float


@dataclass(slots=True)
class IntakeConcurrencyScanner:
    """Derive the live intake concurrency and persist it for the intake factory to read.

    ``static_ceiling`` is the operator's ``issue_implementer_max_concurrent``. It seeds
    the ramp on the first ever pass — the honest ``previous``, since it is what intake
    was actually using — so a cold start steps away from the operator's number rather
    than jumping to whatever an idle box would allow.
    """

    static_ceiling: int
    reserve_gb: float
    per_agent_gb: float
    cadence_minutes: int = 5
    name: str = "intake_concurrency"

    def scan(self) -> list[ScanSignal]:
        from teatree.core.models.resource_pressure_marker import (  # noqa: PLC0415 — deferred: an ORM import at module scope needs the Django app registry, which is not ready when the scanner package is imported
            ResourcePressureMarker,
        )

        try:
            marker = ResourcePressureMarker.load()
        except Exception:
            logger.exception("intake_concurrency: could not load marker — skipping tick")
            return []
        if self._cadence_blocks(marker):
            return []
        decision = self._decide(marker)
        if decision is None or not _persist(marker, decision.concurrency):
            return []
        return self._signals(decision)

    def _decide(self, marker: "ResourcePressureMarker") -> _Decision | None:
        available_mib = effective_available_ram_mib()
        if available_mib is None:
            logger.warning("intake_concurrency: headroom unreadable — intake keeps the static ceiling")
            return None
        previous = marker.adaptive_intake_concurrency or self.static_ceiling
        available_gb = available_mib / _MIB_PER_GB
        adapted = adapt_concurrency(
            available_gb=available_gb,
            in_flight=read_intake_budget("", self.static_ceiling).in_flight,
            previous=previous,
            sizing=BoxSizing(
                cores=available_cpu_count(),
                reserve_gb=self.reserve_gb,
                per_agent_gb=self.per_agent_gb,
            ),
        )
        if adapted is None:
            logger.warning(
                "intake_concurrency: per-agent RAM cost is %s — intake keeps the static ceiling", self.per_agent_gb
            )
            return None
        return _Decision(concurrency=adapted, previous=previous, available_gb=available_gb)

    def _cadence_blocks(self, marker: "ResourcePressureMarker") -> bool:
        """True iff this window's adaptation already ran — the ramp is per window, not per tick."""
        last = marker.adaptive_intake_recorded_at
        if last is None:
            return False
        return (timezone.now() - last).total_seconds() / 60.0 < self.cadence_minutes

    def _signals(self, decision: _Decision) -> list[ScanSignal]:
        if decision.concurrency == decision.previous:
            return []
        direction = "raised" if decision.concurrency > decision.previous else "lowered"
        return [
            ScanSignal(
                kind="resource.intake_concurrency_adapted",
                summary=(
                    f"intake concurrency {direction} {decision.previous} → {decision.concurrency} "
                    f"({decision.available_gb:.1f} GB free, {self.reserve_gb:.0f} GB reserved)"
                ),
                payload={
                    "concurrency": decision.concurrency,
                    "previous": decision.previous,
                    "available_gb": decision.available_gb,
                    "reserve_gb": self.reserve_gb,
                    "per_agent_gb": self.per_agent_gb,
                },
            )
        ]


def _persist(marker: "ResourcePressureMarker", concurrency: int) -> bool:
    try:
        marker.record_adaptive_concurrency(concurrency)
    except Exception:
        logger.exception("intake_concurrency: failed to persist the adapted concurrency")
        return False
    return True


__all__ = ["IntakeConcurrencyScanner"]
