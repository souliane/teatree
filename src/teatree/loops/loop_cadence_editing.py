"""The per-loop cadence write seam — interval XOR wall-clock, bounded by the registry (#3559).

Cadence lived on the ``Loop`` row with no service seam: the Django admin was the
only place to change how often a loop fires. This module is that seam, so the
dashboard (and any future CLI verb) writes cadence through one validated
chokepoint instead of a raw row edit.

Two bounds are enforced, both surfaced to the UI via :func:`cadence_bounds_for`:

*   :data:`ABSOLUTE_MIN_INTERVAL_SECONDS` — nothing may poll faster than this.
*   the registry **cadence floor** — a loop declaring ``cadence_is_floor`` carries
    its own internal cadence and its outer tick is deliberately fast so that inner
    cadence still fires on time. Slowing such a loop past its declared value (or
    moving it to a once-a-day wall-clock time) silently breaks that relationship,
    so both are refused.
"""

import datetime as dt
from dataclasses import dataclass
from typing import Final

from teatree.core.models import Loop
from teatree.loops.registry import iter_loops

#: No loop may be set to fire faster than this — a hard floor against a poll storm.
ABSOLUTE_MIN_INTERVAL_SECONDS: Final = 30


class CadenceEditError(ValueError):
    """A cadence write named an unknown loop or carried a value outside the loop's bounds."""


@dataclass(frozen=True, slots=True)
class CadenceBounds:
    """The interval range a loop may be set to, plus whether a wall-clock time is allowed."""

    min_interval_seconds: int
    max_interval_seconds: int | None
    daily_allowed: bool

    @property
    def note(self) -> str:
        """The one-line explanation the editor renders beside the cadence field."""
        if self.max_interval_seconds is None:
            return f"at least {self.min_interval_seconds}s between runs"
        return (
            f"between {self.min_interval_seconds}s and {self.max_interval_seconds}s — this loop gates its own "
            "work internally, so its outer tick must stay at least this frequent"
        )


def cadence_bounds_for(name: str) -> CadenceBounds:
    """The bounds for *name*, derived from its registry ``MiniLoop`` declaration."""
    floor = _registry_floor_seconds(name)
    return CadenceBounds(
        min_interval_seconds=ABSOLUTE_MIN_INTERVAL_SECONDS,
        max_interval_seconds=floor,
        daily_allowed=floor is None,
    )


def set_loop_cadence(name: str, *, delay_seconds: int | None = None, daily_at: str = "") -> Loop:
    """Set *name*'s cadence to an interval XOR a wall-clock time, validated against its bounds.

    Exactly one of *delay_seconds* / *daily_at* is accepted; the other column is
    cleared so the row can never carry both.
    """
    loop = _require_loop(name)
    bounds = cadence_bounds_for(name)
    wall_clock = daily_at.strip()
    if (delay_seconds is None) == (not wall_clock):
        msg = "set exactly one of an interval or a wall-clock time"
        raise CadenceEditError(msg)
    if wall_clock:
        loop.daily_at = _validated_daily(wall_clock, bounds, name=name)
    else:
        loop.delay_seconds = _validated_interval(delay_seconds, bounds)
        loop.daily_at = None
    loop.save(update_fields=["delay_seconds", "daily_at", "updated_at"])
    return loop


def _validated_interval(delay_seconds: int | None, bounds: CadenceBounds) -> int:
    if delay_seconds is None or delay_seconds < bounds.min_interval_seconds:
        msg = f"interval must be at least {bounds.min_interval_seconds}s"
        raise CadenceEditError(msg)
    if bounds.max_interval_seconds is not None and delay_seconds > bounds.max_interval_seconds:
        msg = (
            f"interval must be at most {bounds.max_interval_seconds}s — this loop gates its own work "
            "internally and its outer tick must stay at least that frequent"
        )
        raise CadenceEditError(msg)
    return delay_seconds


def _validated_daily(raw: str, bounds: CadenceBounds, *, name: str) -> dt.time:
    if not bounds.daily_allowed:
        msg = f"{name!r} gates its own work internally — it needs an interval, not a once-a-day time"
        raise CadenceEditError(msg)
    try:
        return dt.time.fromisoformat(raw)
    except ValueError as exc:
        msg = f"invalid time {raw!r}; use HH:MM"
        raise CadenceEditError(msg) from exc


def _registry_floor_seconds(name: str) -> int | None:
    mini = next((loop for loop in iter_loops() if loop.name == name), None)
    if mini is None or not mini.cadence_is_floor:
        return None
    return mini.default_cadence_seconds


def _require_loop(name: str) -> Loop:
    loop = Loop.objects.filter(name=name).first()
    if loop is None:
        msg = f"no loop named {name!r}"
        raise CadenceEditError(msg)
    return loop


__all__ = [
    "ABSOLUTE_MIN_INTERVAL_SECONDS",
    "CadenceBounds",
    "CadenceEditError",
    "cadence_bounds_for",
    "set_loop_cadence",
]
