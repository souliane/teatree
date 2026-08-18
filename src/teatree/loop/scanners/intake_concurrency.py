"""The resource loop's second job — set intake concurrency from observed headroom (#3992).

Deliberately NOT folded into :class:`~teatree.loop.scanners.resource_pressure.ResourcePressureScanner`:
that one answers "is the box in trouble", this one answers "how much of the box may
intake use", and keeping them apart is what makes this removable on its own. It shares
the resource loop's measurement cadence so the ramp advances once per window rather than
once per tick.

The arithmetic lives in :mod:`teatree.core.intake.concurrency`; this scanner only
supplies the signals (cgroup-aware RAM headroom, the admission-pressure scalar, the
factory's live in-flight count, the core count), persists the answer on the resource
loop's own ledger, and emits when the number MOVES — an adjustment nobody can see is
indistinguishable from a knob nobody turned.

Reading the whole scalar rather than load alone is what gives intake the TOKEN dimension
(#4508): this is the producer, and the one decision where slowing down cannot deadlock
the factory, because the review and ship lanes that drain the pile are untouched.
"""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.utils import timezone

from teatree.core.admission_governor import MachineSignal, pressure_for, read_machine_signal, read_quota_signal
from teatree.core.intake.budget import read_intake_budget
from teatree.core.intake.concurrency import BoxSizing, adapt_concurrency
from teatree.loop.scanners.base import ScanSignal
from teatree.utils.ram_probe import available_cpu_count
from teatree.utils.ram_scope import read_ram_headroom

if TYPE_CHECKING:
    from teatree.core.models.resource_pressure_marker import ResourcePressureMarker

logger = logging.getLogger(__name__)

_MIB_PER_GB = 1024.0


@dataclass(frozen=True, slots=True)
class _Decision:
    """One window's answer, plus both readings and the value it moved from."""

    concurrency: int
    previous: int
    available_gb: float
    load1: float
    cores: int


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
        available_mib = read_ram_headroom().box_watermark_mib
        if available_mib is None:
            logger.warning("intake_concurrency: no box-scoped headroom readable — intake keeps the static ceiling")
            return None
        previous = marker.adaptive_intake_concurrency or self.static_ceiling
        available_gb = available_mib / _MIB_PER_GB
        # The load watermark rides the SAME core count the sizing does, so one box cannot
        # be judged saturated against a core count its own hard cap was not derived from.
        cores = available_cpu_count()
        load1 = read_machine_signal(ram_available_gb=available_gb).load1
        # Memory is deliberately withheld from the scalar HERE: ``additional`` already owns
        # "how many agents fit in memory", and two unsynchronised readers of one quantity
        # drift (#4125). What the scalar adds to this decision is the TOKEN dimension.
        pressure = pressure_for(
            quota=read_quota_signal(), machine=MachineSignal(cores=cores, load1=load1, ram_available_gb=None)
        )
        adapted = adapt_concurrency(
            available_gb=available_gb,
            factory_in_flight=read_intake_budget("", self.static_ceiling).in_flight,
            admission_headroom=max(0.0, 1.0 - pressure.value),
            previous=previous,
            sizing=BoxSizing(
                cores=cores,
                reserve_gb=self.reserve_gb,
                per_agent_gb=self.per_agent_gb,
            ),
        )
        if adapted is None:
            logger.warning(
                "intake_concurrency: per-agent RAM cost is %s — intake keeps the static ceiling", self.per_agent_gb
            )
            return None
        return _Decision(concurrency=adapted, previous=previous, available_gb=available_gb, load1=load1, cores=cores)

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
                    f"({decision.available_gb:.1f} GB free, {self.reserve_gb:.0f} GB reserved, "
                    f"box load {decision.load1:.1f} on {decision.cores} cores)"
                ),
                payload={
                    "concurrency": decision.concurrency,
                    "previous": decision.previous,
                    "available_gb": decision.available_gb,
                    "reserve_gb": self.reserve_gb,
                    "per_agent_gb": self.per_agent_gb,
                    "load1": decision.load1,
                    "cores": decision.cores,
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
