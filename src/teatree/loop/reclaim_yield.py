"""The disk-pressure reading of a freeing payload, and what a pass yield is worth saying.

Split out of :mod:`teatree.loop.mechanical_resources`, which owns the plan and its
execution. The policy itself is :mod:`teatree.core.cleanup.reclaim_pressure`; this is
the loop-side reading of a payload against it, so neither side needs the other`s types.
"""

import logging
from typing import TYPE_CHECKING

from teatree.core.cleanup.reclaim_pressure import below_floor, effective_idle_days, reclaim_is_stalled
from teatree.loop.dispatch import ActionPayload

if TYPE_CHECKING:
    from teatree.core.models.resource_pressure_marker import ResourcePressureMarker

logger = logging.getLogger(__name__)


def pressure_idle_days(payload: ActionPayload) -> float | None:
    """The dormancy this pass may require, decayed by how full the disk actually is."""
    return effective_idle_days(
        free_gb=_payload_float(payload, "free_gb"),
        warn_gb=_payload_float(payload, "disk_warn_free_gb"),
        crit_gb=_payload_float(payload, "disk_crit_free_gb"),
        idle_days=float(payload.get("venv_idle_days", 2)),
    )


def reclaim_yield_steps(marker: "ResourcePressureMarker", *, reclaimed_gb: float, payload: ActionPayload) -> list[str]:
    """Count what this pass returned so a run of empty ones stops being invisible (#4644)."""
    free_gb = _payload_float(payload, "free_gb")
    crit_gb = _payload_float(payload, "disk_crit_free_gb")
    try:
        streak = marker.record_reclaim_pass(
            freed_gb=reclaimed_gb,
            under_critical=below_floor(free_gb=free_gb, crit_gb=crit_gb),
        )
    except Exception:
        logger.exception("free_resources: failed to record the reclaim yield")
        return []
    steps = [f"YIELD {reclaimed_gb:.2f} GB this pass (zero-yield streak {streak})"]
    if reclaim_is_stalled(streak=streak, free_gb=free_gb, crit_gb=crit_gb):
        steps.append(f"STALLED disk reclaim — {streak} consecutive passes freed nothing below the critical floor")
    return steps


def _payload_float(payload: ActionPayload, key: str) -> float | None:
    """``None`` for a reading this payload does not carry — never a guessed default."""
    value = payload.get(key)
    return float(value) if isinstance(value, int | float) else None


__all__ = ["pressure_idle_days", "reclaim_yield_steps"]
