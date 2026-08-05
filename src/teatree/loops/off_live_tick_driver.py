"""The driver for the loops every OTHER driver excludes.

``directive_loop``, ``dream`` and ``outer_loop`` carry ``MiniLoop.off_live_tick``, which
excludes them from BOTH drivers the live plane offers:
:func:`teatree.loops.loop_table._loop_admitted` returns ``False`` before the fan-out
builds a job, and :func:`teatree.loops.chain_membership.timer_chain_loop_names`
intersects ``off_live_tick`` out so the reconciler builds them no ``loop_timer`` chain.
The "own low-frequency cron" their docstrings promised was never installed — leaving all
three with no driver at all, for as long as the box has been running.

This module is that driver, and it stays inside the worker's no-OS-scheduler contract: a
single self-rescheduling :func:`drive_off_live_tick_loops` job on the shared
:data:`~teatree.loops.timer_chains.LOOPS_QUEUE` (the #1796 machinery), seeded by
:func:`teatree.loops.timer_reconciler.ensure_maintenance_chains` at worker startup and
self-perpetuating after that, exactly like its ``reconcile_timers`` /
``render_statusline`` siblings. On each fire it queues ONE
:func:`run_off_live_tick_loop` task per such loop, and each of those runs that loop's
``off_tick_command`` — ``t3 directive tick`` / ``t3 dream tick`` / ``t3 outer tick`` — as
a deadlined subprocess in its own process group. A task apiece rather than three inline:
the heaviest passes in the codebase then neither outlive their ceiling nor hold one
scarce ``loops`` executor thread for the sum of all three.

It applies NO admission gate of its own: every tick command already gates on the single
enable verdict AND its own ``Loop.is_due`` ledger behind an in-flight ``LoopLease``, so a
masked or not-yet-due loop is a cheap SKIP and an at-least-once redelivery is a no-op — a
second gate here would be a drift-prone duplicate of theirs. The ``loop_runner_enabled``
kill-switch IS honoured, exactly as :func:`teatree.loops.timer_chains.loop_timer` honours
it, because this chain drives real work.

The wiring invariant is alarmed rather than merely documented:
:func:`teatree.loops.loop_staleness.driverless_loops` names any ``off_live_tick`` loop
that declares no ``off_tick_command`` — the state this module exists to make impossible.
"""

import datetime as dt
import logging
import sys

from django.tasks import task
from django.utils import timezone

from teatree.loops.deadlined_tick import run_deadlined_argv
from teatree.loops.timer_chains import DAILY_TICK_DEADLINE_SECONDS, LOOPS_QUEUE, LoopRunnerState, read_loop_runner_state

logger = logging.getLogger(__name__)

#: The driver's own cadence. Far finer than the shortest off-live-tick loop cadence
#: (``directive_loop``'s hour), so a due loop waits at most this long, and cheap at that
#: rate because every tick command self-gates on its own ``Loop.is_due`` ledger and
#: returns SKIP without doing work.
DRIVE_INTERVAL_SECONDS = 600

#: The per-command ceiling for one off-live-tick tick subprocess. These are the heaviest
#: passes in the codebase, so they get the daily-tick allowance rather than
#: :func:`~teatree.loops.timer_chains.compute_tick_deadline`'s ``3 x cadence`` — which for
#: the hourly ``directive_loop`` would pin a scarce ``loops`` executor slot for three hours.
DEADLINE_SECONDS = DAILY_TICK_DEADLINE_SECONDS


def off_live_tick_commands() -> list[tuple[str, tuple[str, ...]]]:
    """``(loop name, tick argv tail)`` for every off-live-tick loop that declares a driver.

    A loop that is ``off_live_tick`` and declares no ``off_tick_command`` is absent from
    this list AND from every other driver, so it can never tick — the driverless state
    :func:`teatree.loops.loop_staleness.driverless_loops` alarms on.
    """
    from teatree.loops.registry import iter_loops  # noqa: PLC0415 — deferred: the walk imports every loop module

    return [(loop.name, loop.off_tick_command) for loop in iter_loops() if loop.off_live_tick and loop.off_tick_command]


def _pending_drive() -> bool:
    from django_tasks.base import TaskResultStatus  # noqa: PLC0415 — deferred: heavy/optional dep at call site
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415 — deferred: heavy/optional dep at call site

    return DBTaskResult.objects.filter(
        task_path=drive_off_live_tick_loops.module_path, status=TaskResultStatus.READY
    ).exists()


def _live_run(name: str) -> bool:
    """Whether a run of *name*'s tick command is already queued or executing.

    The per-loop tasks are independent of the driver's own cadence, so a ``dream``
    pass that outlives several drive intervals must not accumulate one queued run per
    interval behind it. A tick command is a cheap SKIP when the loop is not due, so
    this only has to stop a pile-up, not decide admission.
    """
    from django_tasks.base import TaskResultStatus  # noqa: PLC0415 — deferred: heavy/optional dep at call site
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415 — deferred: heavy/optional dep at call site

    rows = DBTaskResult.objects.filter(
        task_path=run_off_live_tick_loop.module_path,
        status__in=[TaskResultStatus.READY, TaskResultStatus.RUNNING],
    )
    return any(row.args_kwargs.get("args") == [name] for row in rows)


@task(queue_name=LOOPS_QUEUE)
def run_off_live_tick_loop(name: str) -> dict[str, int]:
    """Run ONE off-live-tick loop's tick command as a deadlined subprocess, then stop.

    One task per loop rather than three inline in the driver: run back-to-back on a
    single ``loops`` thread, the three heaviest passes in the tree could hold that
    thread for 3 x :data:`DEADLINE_SECONDS`, and on a 2-core box the floor pool is two
    threads — so one drive could occupy half the reactive pool, delaying every
    ``loop_timer`` fire and the 5-minute reconcile chain that repairs them. As
    separate queue entries they interleave with those timers instead of blocking them.

    It never re-arms: :func:`drive_off_live_tick_loops` is the cadence, this is one
    fire of one loop.
    """
    argv_tail = dict(off_live_tick_commands()).get(name)
    if argv_tail is None:
        # The loop was removed (or lost its off_tick_command) since the drive queued
        # this run; the reconciler re-derives the set on the next drive.
        return {"unknown": 1}
    outcome = run_deadlined_argv(
        [sys.executable, "-m", "teatree", *argv_tail],
        label=f"off-live-tick loop {name!r}",
        deadline=DEADLINE_SECONDS,
    )
    return {"driven": 1, "timed_out": int(outcome["timed_out"])}


@task(queue_name=LOOPS_QUEUE)
def drive_off_live_tick_loops() -> dict[str, int]:
    """Re-schedule at its cadence, THEN queue one tick run per off-live-tick loop.

    Step 0 is the kill-switch: a fire while the loop runner is confirmed OFF terminates
    the chain at its source rather than re-arming it, mirroring
    :func:`teatree.loops.timer_chains.loop_timer`. A read that cannot CONFIRM the state
    (``UNREADABLE`` — a transient DB lock) is NOT an OFF: the chain re-arms and runs
    nothing this fire, because terminating on a blip left dream / directive_loop /
    outer_loop with no driver at all until the next worker restart, invisible to every
    alarm. Then self-dedup (another pending fire already carries the chain), then
    successor-FIRST (F6) so a body fault cannot orphan it. A single loop's enqueue
    failure is recorded and stepped over rather than aborting the remaining loops.
    """
    state = read_loop_runner_state()
    if state is LoopRunnerState.OFF:
        return {"halted": 1}
    if _pending_drive():
        return {"deduped": 1}
    drive_off_live_tick_loops.using(run_after=timezone.now() + dt.timedelta(seconds=DRIVE_INTERVAL_SECONDS)).enqueue()
    if state is LoopRunnerState.UNREADABLE:
        logger.warning("off-live-tick driver: kill-switch unreadable — re-armed the chain and drove nothing this fire")
        return {"unconfirmed": 1}

    counts = {"queued": 0, "deduped": 0}
    for name, _argv_tail in off_live_tick_commands():
        try:
            if _live_run(name):
                counts["deduped"] += 1
                continue
            run_off_live_tick_loop.enqueue(name)
        except Exception:
            logger.exception("off-live-tick driver: %r failed to queue; the chain and the other loops survive", name)
            continue
        counts["queued"] += 1
    return counts


def ensure_off_live_tick_driver_chain() -> None:
    """Seed the driver chain head if absent — self-perpetuating after (worker startup)."""
    if not _pending_drive():
        drive_off_live_tick_loops.using(
            run_after=timezone.now() + dt.timedelta(seconds=DRIVE_INTERVAL_SECONDS)
        ).enqueue()
