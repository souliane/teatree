"""The headless WRITE ceiling: cores times the operator's per-core factor, scaled by weekly pace."""

import logging
import math
from dataclasses import dataclass

from teatree.core.admission_pressure import MachineSignal, QuotaSignal, weekly_pace

logger = logging.getLogger(__name__)


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
    from teatree.config.settings import (  # noqa: PLC0415 — deferred: same
        WRITE_CONCURRENCY_PER_CORE,
        WRITE_CONCURRENCY_PER_CORE_MAX,
        WRITE_CONCURRENCY_PER_CORE_MIN,
    )

    try:
        configured = float(get_effective_settings().admission_write_concurrency_per_core)
    except Exception:
        logger.exception("admission_write_concurrency_per_core unreadable — keeping the shipped default")
        return WRITE_CONCURRENCY_PER_CORE
    if not math.isfinite(configured):
        return WRITE_CONCURRENCY_PER_CORE
    return min(WRITE_CONCURRENCY_PER_CORE_MAX, max(WRITE_CONCURRENCY_PER_CORE_MIN, configured))
