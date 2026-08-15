"""Cadence-anchor staleness — "the worker is up, but is anything actually ticking?".

``t3 worker status`` answered three questions — is a worker holding the flock, is
``loop_runner_enabled`` ON, how many ``loop_timer`` rows are READY — and all three
can read green while ZERO work happens. Every gate they cover sits BEFORE the one
that actually decides a tick: the unified admission verdict
(:func:`teatree.loops.loop_table.admitted_loop_names`). A manual mode override to
an all-off mask (the ``off`` mode) leaves the worker RUNNING, the
kill-switch ON and a full set of READY timers, while every ``loop_timer`` fire
returns ``skipped`` and no ``Loop.last_run_at`` moves — a silent freeze the
operator's own health surface reported as healthy for seven hours.

This module is the missing fourth reading, and it is deliberately narrow about
when it cries wolf. Two facts are NOT faults on their own:

*   **Zero loops admitted.** Admission requires ``is_due``, so a healthy fleet that
    just ticked admits nothing for most of any given second. The count is context,
    never the alarm.
*   **One suppressed loop sitting still.** A loop the mode's mask turns off stands
    still by design; reporting that as a failure every hour is a gate people learn to
    ignore.

So a staleness failure is one of two shapes: an **unexplained** stale loop (no deliberate
control plane accounts for it — something is actually broken), or a **frozen fleet**
(every measured loop is behind, which is the seven-hour incident: deliberate, forgotten,
and total). A loop the mode masks off or a ``LoopState`` hold stops is measured and marked
``suppressed`` — never unexplained on its own, yet still counted, because "every loop is
off" IS the incident and a measured set narrowed to the admitted ones would report that
total shutdown as a healthy fleet of zero.

:func:`driverless_loops` is the third, structural reading, and it exists because
staleness alone has a blind spot it cannot close: :func:`_measured_loops` keeps only
live-tick, interval-cadenced rows, so a loop with no driver at all — ``off_live_tick``,
and typically un-admitted besides — is invisible to the very alarm built to catch loops
that are not ticking. Driverlessness is a WIRING property, not a cadence one, so it is
measured off the registry alone and is deliberately NOT gated on enablement or the mask:
a loop nothing can ever drive is a fault whether or not anyone has turned it on yet.

The wording lives here beside the data, not in the CLI — the same single-home rule
:mod:`teatree.loop.statusline_staleness` follows for its stale banner, so every
reader of loop health phrases it identically.
"""

import datetime as dt
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from teatree.core.models import Loop
    from teatree.loops.enable_verdict import EnablePlanes

#: A loop is stale once its anchor is older than ``multiplier x cadence``. Three
#: missed slots — one skipped tick is noise, three in a row is a stopped loop.
#: Deliberately looser than the statusline's 2x render-age gate
#: (:mod:`teatree.loop.statusline_staleness`): that one watches a single file
#: rewritten every tick, this one watches loops whose slots legitimately jitter.
STALE_CADENCE_MULTIPLIER = 3

#: How many stale loops are named before the tail is summarised — a fleet-wide
#: freeze must stay readable in a terminal.
_MAX_NAMED_STALE = 8

_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600
_SECONDS_PER_DAY = 86400

#: The floor under :func:`freeze_cutoff_seconds` — the reconciliation freeze alarm never
#: fires sooner than this however short the cadence. It is the flat cutoff that alarm used
#: to apply to EVERY loop, kept as a floor so scaling to cadence can only ever loosen a
#: fast loop's alarm, never tighten it into a minute-scale hair trigger.
FREEZE_ALARM_FLOOR_SECONDS = _SECONDS_PER_DAY


def format_age(age_seconds: float) -> str:
    """Compact human age — ``45s`` / ``12m`` / ``6h`` / ``3d``."""
    age = int(age_seconds)
    if age < _SECONDS_PER_MINUTE:
        return f"{age}s"
    if age < _SECONDS_PER_HOUR:
        return f"{age // _SECONDS_PER_MINUTE}m"
    if age < _SECONDS_PER_DAY:
        return f"{age // _SECONDS_PER_HOUR}h"
    return f"{age // _SECONDS_PER_DAY}d"


@dataclass(frozen=True, slots=True)
class StaleLoop:
    """One measured loop whose cadence anchor has not moved in more than 3x its cadence."""

    name: str
    cadence_seconds: int
    #: Seconds since ``last_run_at`` — or since the row was created, when it never ran.
    age_seconds: float
    ever_ran: bool
    #: A deliberate control plane accounts for this loop standing still — the enable
    #: verdict refuses it (hold / force-OFF / the active mode's mask).
    suppressed: bool

    @property
    def age_label(self) -> str:
        age = format_age(self.age_seconds)
        return f"last ran {age} ago" if self.ever_ran else f"never run (seeded {age} ago)"

    def as_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "cadence_seconds": self.cadence_seconds,
            "age_seconds": self.age_seconds,
            "ever_ran": self.ever_ran,
            "suppressed": self.suppressed,
        }


@dataclass(frozen=True, slots=True)
class Admission:
    """The resolved mode and the loops its verdict admits right now.

    Context, not a verdict on health: admission also requires ``is_due``, so a fleet
    that ticked a second ago legitimately admits nothing.
    """

    mode: str
    source: str
    admitted: tuple[str, ...]
    #: How many loops the effective verdict admits at all — the denominator that
    #: decides ticks. NOT ``Loop.enabled``'s count, which the preset overrides.
    admitted_total: int

    def as_json(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "mode_source": self.source,
            "admitted": list(self.admitted),
            "admitted_total": self.admitted_total,
        }


@dataclass(frozen=True, slots=True)
class LoopHealth:
    """Whether the loop fleet is actually ticking, and why not when it is not."""

    admission: Admission
    stale: tuple[StaleLoop, ...]
    #: Live-tick interval loops measured this pass — the denominator that makes
    #: "every one of them is behind" a meaningful statement.
    considered: int
    #: Registered loops NO driver reaches — the structural fault staleness cannot see.
    driverless: tuple[str, ...] = ()

    @property
    def unexplained(self) -> tuple[StaleLoop, ...]:
        """Stale loops no deliberate control plane accounts for — something is broken."""
        return tuple(loop for loop in self.stale if not loop.suppressed)

    @property
    def frozen_fleet(self) -> bool:
        """EVERY measured loop is behind its cadence — nothing at all is ticking."""
        return self.considered > 0 and len(self.stale) == self.considered

    @property
    def ok(self) -> bool:
        return not self.frozen_fleet and not self.unexplained and not self.driverless

    def as_json(self) -> dict[str, Any]:
        return {
            **self.admission.as_json(),
            "stale": [loop.as_json() for loop in self.stale],
            "considered": self.considered,
            "frozen_fleet": self.frozen_fleet,
            "driverless": list(self.driverless),
        }

    def lines(self) -> list[str]:
        """The human status block: the admission verdict, then any staleness, then the cause."""
        verdict = self.admission
        rendered = [
            (
                f"mode: {verdict.mode} (source={verdict.source}) — "
                f"{len(verdict.admitted)}/{verdict.admitted_total} admitted loop(s) due"
            )
        ]
        if self.driverless:
            rendered.append(self._driverless_line())
        if not self.frozen_fleet and not self.unexplained:
            rendered.extend(self._suppressed_note())
            return rendered
        reported = self.stale if self.frozen_fleet else self.unexplained
        rendered.append(
            f"STALE: {len(reported)} admitted loop(s) have not ticked in over "
            f"{STALE_CADENCE_MULTIPLIER}x their cadence:"
        )
        rendered.extend(
            f"  {loop.name:<24} every {loop.cadence_seconds}s   {loop.age_label}"
            for loop in reported[:_MAX_NAMED_STALE]
        )
        if len(reported) > _MAX_NAMED_STALE:
            rendered.append(f"  ... and {len(reported) - _MAX_NAMED_STALE} more")
        rendered.append(self._cause_line())
        return rendered

    def _suppressed_note(self) -> list[str]:
        """A quiet, non-failing line for loops that are off exactly as configured."""
        if not self.stale:
            return []
        names = ", ".join(loop.name for loop in self.stale[:_MAX_NAMED_STALE])
        return [f"  ({len(self.stale)} loop(s) idle by configuration: {names})"]

    def _driverless_line(self) -> str:
        """Name the loops nothing can ever drive — a wiring fault, not a cadence one."""
        names = ", ".join(self.driverless)
        return (
            f"FAIL {len(self.driverless)} registered loop(s) have NO driver at all: {names}. "
            "They are off_live_tick (so the live fan-out and the loop-timer chains both skip "
            "them) and declare no off_tick_command for the off-live-tick driver chain to "
            "fire, so they can never tick however enabled or unmasked they are."
        )

    def _cause_line(self) -> str:
        """Name the most likely cause, so a stale reading is actionable rather than alarming."""
        if self.frozen_fleet:
            return (
                f"FAIL the worker is RUNNING but ticking NOTHING — all {self.considered} measured "
                f"loop(s) are behind their cadence under the resolved mode {self.admission.mode!r} "
                f"(source={self.admission.source}). Inspect it with `t3 loop preset show`; "
                "clear a manual override with `t3 loop preset auto`, or pick a narrower "
                "preset with `t3 loop preset use <name>`."
            )
        return (
            "FAIL the worker holds the flock but these loops are not advancing their cadence "
            "anchor, and no control plane explains it. Check "
            "`t3 loop status` and the worker log for a failing tick."
        )


def freeze_cutoff_seconds(cadence_seconds: int | None) -> float:
    """How stale a loop's anchor may get before it is frozen — ``3x`` its OWN cadence.

    The one home for the rule, so the reconciliation alarm and this module's status
    reading can never hold different opinions of what "stale" means. A flat day applied
    to every loop made the weekly ``memory_skim`` stale for ~86% of every week, so the
    line naming a genuinely dead loop arrived beside a permanent false one (#4355).

    ``>=`` this value is stale, matching :data:`STALE_CADENCE_MULTIPLIER`'s own reading —
    three missed slots IS a stopped loop, not the last moment before one. A cadence-less
    (every-tick) loop declares no interval to scale, so it keeps the floor.
    """
    if cadence_seconds is None:
        return float(FREEZE_ALARM_FLOOR_SECONDS)
    return float(max(FREEZE_ALARM_FLOOR_SECONDS, STALE_CADENCE_MULTIPLIER * cadence_seconds))


def driverless_loops() -> tuple[str, ...]:
    """Registered loops NO driver reaches, sorted by name.

    A live-tick loop is driven by the loop-timer chain; an ``off_live_tick`` loop is
    driven by :func:`teatree.loops.off_live_tick_driver.drive_off_live_tick_loops` firing the
    ``off_tick_command`` it declares. An ``off_live_tick`` loop that declares no command
    falls through both and can never tick. This reads the registry alone — see the module
    docstring for why it is deliberately blind to enablement and the mode mask.
    """
    from teatree.loops.registry import iter_loops  # noqa: PLC0415 — deferred: the walk imports every loop module

    return tuple(sorted(loop.name for loop in iter_loops() if loop.off_live_tick and not loop.off_tick_command))


def _measured_loops(now: dt.datetime) -> list["Loop"]:
    """Live-tick interval rows something should be driving, UNION the ones the operator left on.

    Each half alone loses an alarm the other keeps:

    *   chain MEMBERSHIP (:func:`teatree.loops.chain_membership.timer_chain_loop_names`) —
        the loops a timer chain is built for. A mode that forces a column-DISABLED loop ON
        is one of these, and reading the raw ``enabled`` column alone made such a loop
        invisible to the alarm built to catch loops that are not ticking (#4185).
    *   the column-ENABLED live-tick rows — the loops the OPERATOR left on. An all-off mask
        drops every one of them out of membership, so measuring membership alone reports
        the deliberate, forgotten, TOTAL shutdown as a healthy zero-loop fleet: precisely
        the seven-hour incident in this module's docstring (#4196).

    A row in the second half but not the first is standing still ON PURPOSE, which is what
    :func:`_is_suppressed` says of it. It is measured so the FLEET-wide reading can see it,
    never so it can be reported as unexplained.
    """
    from teatree.core.models import Loop  # noqa: PLC0415 — deferred: ORM needs the app registry
    from teatree.loops.chain_membership import (  # noqa: PLC0415 — deferred: ORM-backed
        live_tick_loop_names,
        timer_chain_loop_names,
    )

    members = timer_chain_loop_names(now)
    live_tick = live_tick_loop_names()
    return [
        row
        for row in Loop.objects.all()
        if row.delay_seconds and (row.name in members or (row.enabled and row.name in live_tick))
    ]


def _is_suppressed(row: "Loop", planes: "EnablePlanes") -> bool:
    """Whether a deliberate control plane accounts for *row* standing still.

    The enable verdict itself REFUSES it — a ``LoopState`` hold, an emergency force-OFF,
    or the active mode's mask. Each is an operator turning the loop off; the row is
    measured (the fleet-wide reading needs to see a total shutdown) but never reported
    as unexplained.

    Deriving that from :meth:`~teatree.loops.enable_verdict.EnablePlanes.admits` rather
    than re-walking the planes is what stops it from naming a different set of
    deliberate arms than the verdict does (#4196).
    """
    return not planes.admits(row.name, configured_enabled=row.enabled)


def stale_loops(now: dt.datetime, *, multiplier: int = STALE_CADENCE_MULTIPLIER) -> list[StaleLoop]:
    """Every admitted live-tick interval loop whose anchor is older than ``multiplier x`` its cadence.

    A loop that has NEVER run measures from ``created_at`` instead of its absent
    anchor: a freshly seeded fleet is young and silent by construction (flagging it
    would fail every new install), while a loop that has sat admitted for many
    cadences without ever running is frozen just as surely as one that stopped.
    Sorted by name so the status output is stable between runs.
    """
    from teatree.loops.enable_verdict import EnablePlanes  # noqa: PLC0415 — deferred: ORM-backed resolver

    # The SAME seam ``_measured_loops`` resolves membership through, so the verdict that
    # excuses a loop and the verdict that made it a member are one object.
    planes = EnablePlanes.resolve(now)
    stale = [
        StaleLoop(
            name=row.name,
            cadence_seconds=row.delay_seconds,
            age_seconds=age,
            ever_ran=row.last_run_at is not None,
            suppressed=_is_suppressed(row, planes),
        )
        for row in _measured_loops(now)
        if (age := (now - (row.last_run_at or row.created_at)).total_seconds()) > multiplier * row.delay_seconds
    ]
    return sorted(stale, key=lambda loop: loop.name)


def admission(now: dt.datetime) -> Admission:
    """The resolved mode plus the loops its unified verdict admits at *now*.

    Reads the SAME verdict the loop-timer chain gates on
    (:func:`teatree.loops.loop_table.admitted_loop_names`), so the number the
    operator is shown is the number that decides whether a tick happens — it can
    never drift into a second, friendlier opinion. The denominator is the chain
    membership, for the same reason: a count of ``Loop.enabled`` rows is not the count
    that decides ticks once a preset overrides the column.
    """
    from teatree.loops.chain_membership import timer_chain_loop_names  # noqa: PLC0415 — deferred: ORM-backed
    from teatree.loops.enable_verdict import EnablePlanes  # noqa: PLC0415 — deferred: ORM-backed resolver
    from teatree.loops.loop_table import admitted_loop_names  # noqa: PLC0415 — deferred: loaded at status time

    resolved = EnablePlanes.resolve(now).resolved
    return Admission(
        mode=resolved.name,
        source=resolved.source,
        admitted=tuple(sorted(admitted_loop_names(now))),
        admitted_total=len(timer_chain_loop_names(now)),
    )


def loop_health(now: dt.datetime) -> LoopHealth:
    """The one loop-health reading ``t3 worker status`` reports and exits on."""
    return LoopHealth(
        admission=admission(now),
        stale=tuple(stale_loops(now)),
        considered=len(_measured_loops(now)),
        driverless=driverless_loops(),
    )


__all__ = [
    "STALE_CADENCE_MULTIPLIER",
    "Admission",
    "LoopHealth",
    "StaleLoop",
    "admission",
    "driverless_loops",
    "format_age",
    "loop_health",
    "stale_loops",
]
