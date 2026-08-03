"""Intake concurrency, derived from observed headroom rather than hand-set (#3992).

Issue intake claimed against a fixed number. Throughput is ``concurrency /
ticket-duration`` and concurrency was the only term under direct control, so the one
knob that mattered was set once, in advance, for conditions that vary by the hour: it
wasted capacity on an idle box and was too generous exactly when several agents ran full
test suites at once — the memory-tight moments where contention produces red tests that
are not real failures. The resource loop already watches the box; this module is what
lets it own the number.

The decision is PURE and lives here; the resource loop's scanner supplies the signals
and persists the answer, and :func:`resolve_intake_concurrency` is what the intake
factory reads. Two properties are deliberate:

*   **A reserve, not the last gigabyte.** ``reserve_gb`` is held back, so ``additional``
    goes NEGATIVE while memory still remains — tightening therefore reduces the limit
    *before* the box reaches its ceiling, rather than at it. Sizing to the edge turns a
    burst into an OOM, and an OOM of the worker is a factory outage, not a slow build.
*   **Down fast, up slowly.** A drop is adopted whole; a rise advances one step per
    adjustment. That keeps the system away from oscillation and errs toward the cheaper
    mistake.

Every uncertain input yields ``None`` — *no opinion* — and the reader then keeps the
operator's static ``issue_implementer_max_concurrent`` verbatim. A governor that cannot
read its own signals must never wedge the factory, and it must never clamp on a stale
answer either: the persisted value expires after :data:`ADAPTIVE_FRESHNESS`, so a
stopped resource loop reverts to the static setting instead of freezing the number.
"""

import logging
import math
from dataclasses import dataclass
from datetime import timedelta

logger = logging.getLogger(__name__)

#: The absolute ceiling a misread RAM figure cannot exceed, as a multiple of cores. RAM
#: is the live constraint — this only bounds a bogus reading, so it sits well above the
#: RAM-derived answer on a healthy box.
HARD_CAP_PER_CORE = 0.5

#: Never below one: zero would wedge intake entirely, and the in-flight gate already
#: blocks a new claim whenever the in-flight count has reached the limit.
MIN_CONCURRENCY = 1

#: A persisted value older than this is ignored. Comfortably past the resource loop's
#: 5-minute measurement cadence, so an ordinary tick gap never drops the adaptation,
#: while a loop that has actually stopped stops being trusted within the half hour.
ADAPTIVE_FRESHNESS = timedelta(minutes=30)


@dataclass(frozen=True, slots=True)
class BoxSizing:
    """What one admitted ticket costs this box, and what is held back from it."""

    cores: int
    reserve_gb: float
    per_agent_gb: float

    @property
    def hard_cap(self) -> int:
        return max(MIN_CONCURRENCY, math.floor(max(1, self.cores) * HARD_CAP_PER_CORE))


def adapt_concurrency(
    *,
    available_gb: float | None,
    in_flight: int,
    previous: int,
    sizing: BoxSizing,
) -> int | None:
    """The concurrency *observed headroom* supports now, or ``None`` for no opinion.

    *available_gb* is what this process may actually still allocate (cgroup-aware);
    ``None`` means the probe could not read it. *in_flight* is what is already running,
    so ``in_flight + additional`` is a statement about the whole box rather than about
    free memory alone. *previous* is the last adopted value and supplies the ramp.
    """
    if available_gb is None or sizing.per_agent_gb <= 0:
        return None
    additional = math.floor((available_gb - sizing.reserve_gb) / sizing.per_agent_gb)
    target = _clamp(in_flight + additional, sizing.hard_cap)
    if target < previous:
        return target
    return _clamp(min(target, previous + 1), sizing.hard_cap)


def _clamp(value: int, hard_cap: int) -> int:
    return max(MIN_CONCURRENCY, min(value, hard_cap))


def resolve_intake_concurrency(static_ceiling: int, *, overlay: str = "") -> int:
    """The live intake limit: the resource loop's answer, else *static_ceiling* verbatim.

    The static setting is the FALLBACK, not a bound — the whole point is that the box's
    own reading may be higher than a number chosen in advance. It is returned unchanged
    whenever the adaptation has nothing trustworthy to say: the kill-switch is off, no
    value has ever been computed, the ledger row is unreadable, or the last reading has
    aged past :data:`ADAPTIVE_FRESHNESS`.
    """
    from django.utils import timezone  # noqa: PLC0415 — deferred: Django app-registry read at call time

    from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: avoids a config import cycle
    from teatree.core.models.resource_pressure_marker import ResourcePressureMarker  # noqa: PLC0415 — deferred: ORM

    try:
        if not get_effective_settings(overlay or None).adaptive_intake_concurrency_enabled:
            return static_ceiling
        marker = ResourcePressureMarker.objects.filter(singleton=True).first()
    except Exception:
        logger.exception("adaptive intake concurrency unreadable — keeping the static ceiling")
        return static_ceiling
    if marker is None or marker.adaptive_intake_concurrency is None or marker.adaptive_intake_recorded_at is None:
        return static_ceiling
    if timezone.now() - marker.adaptive_intake_recorded_at > ADAPTIVE_FRESHNESS:
        logger.warning(
            "adaptive intake concurrency last written %s is stale — keeping the static ceiling %d",
            marker.adaptive_intake_recorded_at,
            static_ceiling,
        )
        return static_ceiling
    return max(MIN_CONCURRENCY, marker.adaptive_intake_concurrency)


__all__ = [
    "ADAPTIVE_FRESHNESS",
    "HARD_CAP_PER_CORE",
    "MIN_CONCURRENCY",
    "BoxSizing",
    "adapt_concurrency",
    "resolve_intake_concurrency",
]
