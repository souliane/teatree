"""The per-loop cadence write seam — interval XOR wall-clock, bounded by the registry (#3559).

Cadence lived on the ``Loop`` row with no service seam: the Django admin was the
only place to change how often a loop fires. This module is that seam, so the
dashboard (and any future CLI verb) writes cadence through one validated
chokepoint instead of a raw row edit.

Two bounds are enforced, both surfaced to the UI via :func:`cadence_bounds_for`:

*   the cadence GRID (:data:`CADENCE_STEP_SECONDS`, derived from the timer chain's own poll
    floor) — nothing may poll faster than one step, and every interval must be a multiple of
    one, so the configured number is the interval actually observed rather than a number the
    machinery rounds off under load. Rows written before the grid are REPORTED by
    :func:`off_grid_cadences`, never rewritten.
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
from teatree.loops.timer_chains import IDLE_POLL_FLOOR_SECONDS

#: The cadence GRID: a loop may be set to any positive multiple of this, and nothing faster.
#:
#: DERIVED from the timer chain's own poll floor rather than chosen (#4079). Every loop rides
#: a self-rescheduling ``loop_timer`` chain whose successor is enqueued at
#: ``max(next_slot, now + IDLE_POLL_FLOOR_SECONDS)`` and is refined DOWN to the precise slot
#: only after a tick that actually moved the cadence anchor. Every other path — a
#: held/disabled/not-due loop, a faulted tick whose anchor never moved, a cadence-less loop —
#: leaves the successor on that floor. So a sub-floor cadence is honoured only while nothing
#: goes wrong, and an off-grid one lands on a grid boundary as soon as something does: 31s
#: behaves as 31s or 60s depending on the last tick, which is not a cadence anyone configured.
#:
#: The previous value was an invented 30 whose own comment called it "a hard floor against a
#: poll storm" — a sanity gate matching nothing in the machinery, and below the real floor in
#: every case. Deriving it here is what makes the number in the editor the number observed.
CADENCE_STEP_SECONDS: Final = IDLE_POLL_FLOOR_SECONDS

#: No loop may be set to fire faster than this. The first point on the grid.
ABSOLUTE_MIN_INTERVAL_SECONDS: Final = CADENCE_STEP_SECONDS


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
        """This row's OWN bounds explanation, or ``""`` when they are the global ones.

        Empty for an ordinary loop (#4079). The floor and the grid are the same for every
        loop, so a per-row sentence carrying them says nothing ABOUT THAT ROW — it was the
        identical line repeated down the whole table, which the operator reads past. The
        table states the global rule once as a legend instead.

        A ``cadence_is_floor`` loop is the genuine exception: its registry MAXIMUM is
        row-specific, nothing else on the page carries it, and slowing the loop past it
        silently breaks the relationship its declaration exists to keep. That one still
        speaks for itself, on its own row.
        """
        if self.max_interval_seconds is None:
            return ""
        return (
            f"at most {self.max_interval_seconds}s — this loop gates its own work internally, "
            "so its outer tick must stay at least this frequent"
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
        msg = (
            f"interval must be at least {bounds.min_interval_seconds}s — the loop timer chain "
            "polls on that floor, so anything faster is not a cadence the loop can keep"
        )
        raise CadenceEditError(msg)
    if delay_seconds % CADENCE_STEP_SECONDS:
        # Refused rather than rounded: silently snapping would store a number the operator
        # never typed, and the whole point is that the configured value equals the observed one.
        below = delay_seconds - delay_seconds % CADENCE_STEP_SECONDS
        msg = (
            f"interval must be a multiple of {CADENCE_STEP_SECONDS}s — the loop timer chain polls on "
            f"that grid, so {delay_seconds}s would run as {below}s or {below + CADENCE_STEP_SECONDS}s "
            f"depending on the last tick. Use {below}s or {below + CADENCE_STEP_SECONDS}s."
        )
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


def is_off_grid(delay_seconds: int | None) -> bool:
    """Whether a stored interval sits between two grid points (``None`` — a daily row — never does).

    The ONE predicate behind both the DB-reading report and the dashboard's own listing, so a
    row the table calls off-grid is exactly a row :func:`off_grid_cadences` would name.
    """
    return delay_seconds is not None and bool(delay_seconds % CADENCE_STEP_SECONDS)


def off_grid_cadences() -> tuple[tuple[str, int], ...]:
    """Every stored loop whose interval is NOT on the cadence grid — ``(name, delay_seconds)``.

    A READ. Rows that predate the grid keep the value an operator typed; this reports them so
    the operator decides, and never rewrites one (#4079). A migration that rounded 45s to 60s
    would destroy a stated intent without telling anyone, and the operator is the only party
    who knows whether 45 meant "about a minute" or something load-bearing.

    A ``daily_at`` row carries no interval, so it is off the grid's scope entirely rather than
    off the grid. Sorted by name so the report is deterministic.
    """
    rows = Loop.objects.values_list("name", "delay_seconds").order_by("name")
    return tuple((name, delay) for name, delay in rows if delay is not None and is_off_grid(delay))


def _require_loop(name: str) -> Loop:
    loop = Loop.objects.filter(name=name).first()
    if loop is None:
        msg = f"no loop named {name!r}"
        raise CadenceEditError(msg)
    return loop


__all__ = [
    "ABSOLUTE_MIN_INTERVAL_SECONDS",
    "CADENCE_STEP_SECONDS",
    "CadenceBounds",
    "CadenceEditError",
    "cadence_bounds_for",
    "is_off_grid",
    "off_grid_cadences",
    "set_loop_cadence",
]
