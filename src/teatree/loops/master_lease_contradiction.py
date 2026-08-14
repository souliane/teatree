"""Is ``t3-master`` unheld while loops are demonstrably still ticking? (#4253).

The two facts the owner-gated reactive cycles collapsed into one. A cycle that reads the
lease alone can say the slot is unheld and nothing more; it cannot say the factory is
idle, and on the box that produced the ticket it was not — five loops were inside their
own cadence, the worker flock was held, and two cycles no-oped every beat with the only
notice a log line no person reads.

Reading BOTH is what makes the state reportable, so this module is the only place they
are read together and :mod:`teatree.cli.doctor.checks_loop` is its caller. An unheld
lease with NOTHING ticking is not this finding — that is an honestly idle box, and a
stopped chain is already
:func:`teatree.loops.schedule_liveness.unscheduled_loops`'s to report.

Only INTERVAL loops count as evidence, and only ones that ticked within
:data:`LIVE_TICK_CEILING_SECONDS` AND inside :data:`TICK_FRESHNESS_MULTIPLE` of their own
cadence. A daily loop twenty hours into its schedule satisfies the cadence test while
saying nothing about whether anything is driving ticks right now, which is the only claim
this evidence is allowed to make.
"""

import datetime as dt
from dataclasses import dataclass

from teatree.core.loop_lease_manager import T3_MASTER_SLOT

#: A loop whose last run is within this multiple of its OWN interval ticked on schedule.
#: Two beats of slack, matching the stale-tick health signal's "overrun > 2x cadence".
TICK_FRESHNESS_MULTIPLE = 2

#: How many ticking loop names the finding names before summarising the rest as a count.
NAMED_LOOPS_IN_FINDING = 5

#: Wall-clock ceiling on evidence, independent of any loop's own cadence. The claim this
#: evidence backs is "ticks are being driven RIGHT NOW", and a daily loop twenty hours
#: into its cadence is perfectly on schedule while saying nothing about right now — so
#: the cadence multiple alone admitted a six-hour-idle box as "ticking". The measured
#: outage's ages were 0-282 s, so this is generous against the shape it must catch.
LIVE_TICK_CEILING_SECONDS = 900


@dataclass(frozen=True, slots=True)
class UnheldMasterLease:
    """The contradiction: no live ``t3-master`` owner, yet loops are ticking on cadence."""

    ticking_loops: tuple[str, ...]
    freshest_tick_seconds: float

    def describe(self) -> str:
        """The evidence line the doctor FAIL quotes: how many loops ticked, and how recently."""
        hidden = len(self.ticking_loops) - NAMED_LOOPS_IN_FINDING
        shown = ", ".join(self.ticking_loops[:NAMED_LOOPS_IN_FINDING])
        more = f" and {hidden} more" if hidden > 0 else ""
        return (
            f"{len(self.ticking_loops)} loop(s) are ticking on cadence ({shown}{more}), "
            f"freshest {self.freshest_tick_seconds:.0f}s ago"
        )


def ticking_interval_loops(now: dt.datetime) -> tuple[tuple[str, float], ...]:
    """Every enabled interval loop that ran recently AND within its own cadence.

    Both conditions are required, because each admits a box the other rejects: the
    cadence multiple alone counts a daily loop twenty hours into its schedule (on
    cadence, but no evidence of a tick right now), and the wall-clock ceiling alone
    would count a 60 s loop ten cadences overrun.

    Returned as ``(name, seconds_since_run)`` pairs so the caller reports the evidence it
    decided on rather than re-deriving it from a second read.
    """
    from teatree.core.models import Loop  # noqa: PLC0415 — deferred: ORM import needs the app registry

    fresh: list[tuple[str, float]] = []
    for row in Loop.objects.filter(enabled=True, delay_seconds__isnull=False).exclude(last_run_at=None):
        age = row.seconds_since_run(now)
        if age is None or age > LIVE_TICK_CEILING_SECONDS:
            continue
        if age <= TICK_FRESHNESS_MULTIPLE * max(row.delay_seconds or 0, 1):
            fresh.append((row.name, age))
    return tuple(sorted(fresh))


def unheld_master_lease_with_live_ticks(now: dt.datetime) -> UnheldMasterLease | None:
    """The #4253 finding, or ``None`` when the two facts do not contradict each other.

    ``None`` covers both benign readings: a held lease (the ordinary state) and an unheld
    lease with nothing ticking (an idle box, whose stopped chains belong to
    :func:`~teatree.loops.schedule_liveness.unscheduled_loops`).
    """
    from teatree.core.models import LoopLease  # noqa: PLC0415 — deferred: ORM import needs the app registry

    if LoopLease.objects.ownership_status(T3_MASTER_SLOT).is_live:
        return None
    ticking = ticking_interval_loops(now)
    if not ticking:
        return None
    return UnheldMasterLease(
        ticking_loops=tuple(name for name, _ in ticking),
        freshest_tick_seconds=min(age for _, age in ticking),
    )


__all__ = [
    "LIVE_TICK_CEILING_SECONDS",
    "TICK_FRESHNESS_MULTIPLE",
    "UnheldMasterLease",
    "ticking_interval_loops",
    "unheld_master_lease_with_live_ticks",
]
