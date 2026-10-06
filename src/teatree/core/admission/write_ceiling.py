"""The headless WRITE ceiling: cores times the operator's per-core factor, scaled by weekly pace."""

import logging
import math
from dataclasses import dataclass

from teatree.core.admission_pressure import MachineSignal, QuotaSignal, weekly_pace

logger = logging.getLogger(__name__)

#: The shipped ``admission_write_concurrency_per_core``: WRITE concurrency as a function of
#: cores, not a magic number, so a bigger box scales up automatically. 8 cores → 4.
#:
#: This was 0.25 (8 cores → 2), calibrated against the meltdown recorded on
#: :data:`~teatree.core.admission_governor.TOTAL_TEST_WORKERS_PER_CORE` — which names its
#: own cause: "the per-agent expansion is the melt driver, NOT the agent count". That driver
#: is now bounded independently by
#: :func:`~teatree.core.admission_governor.per_agent_test_workers`, which divides a
#: ``cores * 2`` TOTAL worker budget by the active-agent count, so total workers stay
#: bounded however many agents run. The old value was set before that guard existed and
#: priced agent count as if it were the hazard.
#:
#: Raising it is safe to attempt rather than safe by assertion: the load brake still denies
#: above ``BRAKE_LOAD_PER_CORE * cores`` and holds to ``RESUME_LOAD_PER_CORE * cores``, so an
#: over-aggressive value throttles itself instead of melting the box. Measured at the change:
#: load 13.4/15.9/16.5 on 8 cores against a deny watermark of 40, 14 GB RAM free.
WRITE_CONCURRENCY_PER_CORE = 0.5
WRITE_CONCURRENCY_PER_CORE_MIN = 0.25
WRITE_CONCURRENCY_PER_CORE_MAX = 2.0


@dataclass(frozen=True)
class AdmissionCeiling:
    """The WRITE ceiling with the parts it is derived from, so a status surface can show why."""

    cores: int
    per_core: float
    pace: float | None

    @property
    def machine(self) -> int:
        """The core-derived part, floored at 1 — the bound that needs NO quota signal."""
        return self._seats(max(1, self.cores) * self.per_core)

    @property
    def value(self) -> int:
        if self.pace is None:
            return self.machine
        return self._seats(self.machine * min(1.0, self.pace))

    @staticmethod
    def _seats(product: float) -> int:
        """Rounded before the floor so float error (25 x 1.16 = 28.999...) never costs a seat."""
        return max(1, math.floor(round(product, 9)))


def admission_ceiling(quota: QuotaSignal, machine: MachineSignal) -> AdmissionCeiling:
    """An unread quota drops only the weekly-pace scaling, never the machine bound (#4097)."""
    return AdmissionCeiling(
        cores=machine.cores,
        per_core=_write_concurrency_per_core(),
        pace=weekly_pace(quota) if quota.fresh else None,
    )


def _write_concurrency_per_core() -> float:
    """The operator's per-core factor, clamped; an unreadable setting keeps the shipped default."""
    from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: avoids a config import cycle

    try:
        configured = float(get_effective_settings().admission_write_concurrency_per_core)
    except Exception:
        logger.exception("admission_write_concurrency_per_core unreadable — keeping the shipped default")
        return WRITE_CONCURRENCY_PER_CORE
    if not math.isfinite(configured):
        return WRITE_CONCURRENCY_PER_CORE
    return min(WRITE_CONCURRENCY_PER_CORE_MAX, max(WRITE_CONCURRENCY_PER_CORE_MIN, configured))
