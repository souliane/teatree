"""Scheduledness — is a loop still ON a timer chain, or has it silently stopped?

:mod:`teatree.loops.loop_staleness` reads the cadence ANCHOR: has ``last_run_at``
moved recently. That reading has a blind spot it cannot close on its own, and
souliane/teatree#4140 fell straight through it — ``issue_implementer`` stopped
firing for 61 minutes while ``t3 loop list`` reported ``last 7m04s … in 22m55s``,
because a manual ``t3 loops tick`` bumps the anchor WITHOUT restoring the chain.
A recent anchor is evidence a tick ran; it is not evidence one is scheduled.

This module is that second reading. A timer-chained loop is SCHEDULED when it has
a READY ``loop_timer`` row (a queued successor) or a RUNNING one still inside its
tick deadline. A RUNNING row past ``compute_tick_deadline + STUCK_GRACE_SECONDS``
is a corpse — the same predicate :func:`~teatree.loops.timer_reconciler.
ensure_loop_timers` uses to reap it — so a loop whose only rows are corpses is
carrying no chain at all and will never fire again on its own.

The enumeration is :func:`~teatree.loops.timer_reconciler.timer_chain_loop_names`,
which already excludes ``off_live_tick`` loops (``directive_loop``, ``dream``,
``outer_loop``). Those are driven by :mod:`teatree.loops.off_live_tick_driver`
firing their own tick command, never by a worker timer, so having no timer row is
their correct steady state — exposing them here would be a permanent false alarm.

The ``loop_runner_enabled`` kill-switch is this reading's PRECONDITION, gated the way
:func:`teatree.cli.doctor.checks_runtime._check_worker_running` and
:class:`teatree.cli.doctor.self_heal._Probe` gate theirs. Step 0 of
:func:`~teatree.loops.timer_chains.loop_timer` halts WITHOUT enqueueing a successor
precisely to terminate every chain at its source, and ``Loop.enabled`` is untouched —
so an OFF switch drains the whole fleet into the state this module reads as stopped.
Naming those loops would red-line an operator's own decision and hand them remediation
for a fault they did not have. The OFF state itself is reported once, by the surface
that owns it (:func:`teatree.cli.doctor.checks_slack_roundtrip._probe_answer_pipeline`),
never once per enabled loop.

**Disclosed limit.** The corpse predicate keys on a RUNNING row outliving
``compute_tick_deadline + STUCK_GRACE_SECONDS``, so a chain dropped mid-window is named
only once that grace expires — not at the moment of the drop. Between the two, this
reading still reports the loop as scheduled. It bounds detection latency, not coverage:
the chain is still named, and the reaper works off the same predicate.
"""

import datetime as dt
from dataclasses import dataclass

from teatree.loops.timer_chains import compute_tick_deadline, loop_runner_enabled
from teatree.loops.timer_reconciler import STUCK_GRACE_SECONDS, timer_chain_loop_names


def loop_timers_by_name(status: str) -> dict[str, list]:
    """Every ``loop_timer`` row in *status*, grouped by the loop name in its args."""
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415 — deferred: heavy/optional dep at call site

    from teatree.loops.timer_chains import _loop_timer_path  # noqa: PLC0415 — deferred: loaded at tick time

    grouped: dict[str, list] = {}
    for row in DBTaskResult.objects.filter(task_path=_loop_timer_path(), status=status):
        args = row.args_kwargs.get("args") or []
        if args:
            grouped.setdefault(args[0], []).append(row)
    return grouped


def is_stranded(result, loop_row, now: dt.datetime) -> bool:  # noqa: ANN001 — untyped by design: a duck-typed handle passed positionally
    """Whether a RUNNING timer has outlived its tick deadline + grace (a dead worker).

    The single corpse predicate: the reaper (:func:`~teatree.loops.timer_reconciler.
    ensure_loop_timers`) and this alarm consume it together, so they can never
    disagree about which rows are dead.
    """
    if result.started_at is None:
        return False
    limit = compute_tick_deadline(loop_row) + STUCK_GRACE_SECONDS
    return result.started_at < now - dt.timedelta(seconds=limit)


@dataclass(frozen=True, slots=True)
class UnscheduledLoop:
    """One enabled, timer-chained loop that no live timer carries."""

    name: str
    corpse_timers: int

    @property
    def reason(self) -> str:
        if self.corpse_timers:
            return f"{self.corpse_timers} RUNNING timer row(s) past their tick deadline, no queued successor"
        return "no loop_timer row at all — neither READY nor RUNNING"


def unscheduled_loops(now: dt.datetime) -> tuple[UnscheduledLoop, ...]:
    """Every enabled, timer-chained loop carrying neither a READY timer nor a live tick.

    A loop named here has silently stopped: nothing will fire it again until the
    reconciler re-heads its chain, and every cadence surface still reads healthy.

    Empty while the ``loop_runner_enabled`` kill-switch is OFF — a drained fleet is
    then the operator's own decision, not a stopped chain (see the module docstring).
    """
    from django_tasks.base import TaskResultStatus  # noqa: PLC0415 — deferred: heavy/optional dep at call site

    from teatree.core.models import Loop  # noqa: PLC0415 — deferred: ORM import needs the app registry

    if not loop_runner_enabled():
        return ()
    chain_names = timer_chain_loop_names()
    if not chain_names:
        return ()
    loops = {row.name: row for row in Loop.objects.filter(name__in=chain_names)}
    ready_by_name = loop_timers_by_name(TaskResultStatus.READY)
    running_by_name = loop_timers_by_name(TaskResultStatus.RUNNING)

    stalled: list[UnscheduledLoop] = []
    for name in sorted(chain_names):
        if ready_by_name.get(name):
            continue
        running = running_by_name.get(name, [])
        corpses = [row for row in running if is_stranded(row, loops[name], now)]
        if len(corpses) < len(running):
            continue
        stalled.append(UnscheduledLoop(name=name, corpse_timers=len(corpses)))
    return tuple(stalled)
