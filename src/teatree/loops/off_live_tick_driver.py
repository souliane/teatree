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
``render_statusline`` siblings. On each fire it runs the ``off_tick_command`` every such
loop declares — ``t3 directive tick`` / ``t3 dream tick`` / ``t3 outer tick`` — as a
deadlined subprocess in its own process group, so the heaviest passes in the codebase can
neither wedge a scarce ``loops`` executor thread nor outlive their ceiling.

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
from teatree.loops.timer_chains import DAILY_TICK_DEADLINE_SECONDS, LOOPS_QUEUE, loop_runner_enabled

logger = logging.getLogger(__name__)

#: The driver's own cadence. Far finer than the shortest off-live-tick loop cadence
#: (``directive_loop``'s hour), so a due loop waits at most this long, and cheap at that
#: rate because every tick command self-gates on its own ``Loop.is_due`` ledger and
#: returns SKIP without doing work.
DRIVE_INTERVAL_SECONDS = 600

#: The DEFAULT per-command ceiling for one off-live-tick tick subprocess. These are the
#: heaviest passes in the codebase, so they get the daily-tick allowance rather than
#: :func:`~teatree.loops.timer_chains.compute_tick_deadline`'s ``3 x cadence`` — which for
#: the hourly ``directive_loop`` would pin a scarce ``loops`` executor slot for three hours.
#:
#: A loop may override it with ``MiniLoop.off_tick_deadline_seconds``, and ``dream`` does:
#: a shared value forced its 1800s IN-PASS budget and this 1800s external SIGKILL to be
#: EQUAL, leaving the pass no headroom to finish anything after its distiller. ``dream``
#: declares its own ceiling above its budget so the kill is a backstop; the other two
#: off-live-tick loops declare none and are unchanged (neither has ever reached this
#: deadline — every kill in the deploy's logs is ``dream``).
DEADLINE_SECONDS = DAILY_TICK_DEADLINE_SECONDS


def off_live_tick_commands() -> list[tuple[str, tuple[str, ...], float]]:
    """``(loop name, tick argv tail, deadline)`` for every off-live-tick loop with a driver.

    A loop that is ``off_live_tick`` and declares no ``off_tick_command`` is absent from
    this list AND from every other driver, so it can never tick — the driverless state
    :func:`teatree.loops.loop_staleness.driverless_loops` alarms on.

    The deadline is the loop's own ``off_tick_deadline_seconds`` when it declares one and
    :data:`DEADLINE_SECONDS` otherwise, resolved here so the driver body reads one list
    rather than walking the registry twice.
    """
    from teatree.loops.registry import iter_loops  # noqa: PLC0415 — deferred: the walk imports every loop module

    return [
        (loop.name, loop.off_tick_command, float(loop.off_tick_deadline_seconds or DEADLINE_SECONDS))
        for loop in iter_loops()
        if loop.off_live_tick and loop.off_tick_command
    ]


def _pending_drive() -> bool:
    from django_tasks.base import TaskResultStatus  # noqa: PLC0415 — deferred: heavy/optional dep at call site
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415 — deferred: heavy/optional dep at call site

    return DBTaskResult.objects.filter(
        task_path=drive_off_live_tick_loops.module_path, status=TaskResultStatus.READY
    ).exists()


@task(queue_name=LOOPS_QUEUE)
def drive_off_live_tick_loops() -> dict[str, int]:
    """Re-schedule at its cadence, THEN run each off-live-tick loop's own tick command.

    Step 0 is the kill-switch: a fire while the loop runner is OFF terminates the chain
    at its source rather than re-arming it, mirroring
    :func:`teatree.loops.timer_chains.loop_timer`. Then self-dedup (another pending fire
    already carries the chain), then successor-FIRST (F6) so a body fault cannot orphan
    it. A single command's failure is recorded and stepped over rather than aborting the
    remaining loops.
    """
    if not loop_runner_enabled():
        return {"halted": 1}
    if _pending_drive():
        return {"deduped": 1}
    drive_off_live_tick_loops.using(run_after=timezone.now() + dt.timedelta(seconds=DRIVE_INTERVAL_SECONDS)).enqueue()

    counts = {"driven": 0, "timed_out": 0}
    for name, argv_tail, deadline in off_live_tick_commands():
        try:
            outcome = run_deadlined_argv(
                [sys.executable, "-m", "teatree", *argv_tail],
                label=f"off-live-tick loop {name!r}",
                deadline=deadline,
            )
        except Exception:
            logger.exception("off-live-tick driver: %r failed to run; the chain and the other loops survive", name)
            continue
        counts["driven"] += 1
        counts["timed_out"] += int(outcome["timed_out"])
    return counts


def ensure_off_live_tick_driver_chain() -> None:
    """Seed the driver chain head if absent — self-perpetuating after (worker startup)."""
    if not _pending_drive():
        drive_off_live_tick_loops.using(
            run_after=timezone.now() + dt.timedelta(seconds=DRIVE_INTERVAL_SECONDS)
        ).enqueue()
