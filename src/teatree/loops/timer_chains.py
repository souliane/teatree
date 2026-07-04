"""Self-rescheduling loop-timer chains — durable, crash-surviving loop cadence (#1796).

Replaces the in-memory beat with django-tasks ``run_after`` rows: exactly one
pending ``loop_timer(name)`` task per enabled :class:`Loop` row on the dedicated
``loops`` queue is a durable timer that survives a crash — the DB row IS the
scheduled fire. When a worker executor drains it (its ``run_after`` has elapsed)
the task runs a five-step body that re-schedules its own successor BEFORE doing
the tick work, so a crash mid-tick always leaves a queued successor and the chain
never stalls.

The tick body, five fixed steps:

Step 1 — self-dedup: a second pending ``loop_timer`` for the same loop already
carries the chain, so this one stops without chaining (collapses duplicates to one
— the "exactly one pending timer per loop" invariant self-heals).

Step 2 — successor-first re-enqueue: schedule the next timer at a conservative
``run_after`` BEFORE running the tick, so a crash during the tick leaves a queued
successor (crash-safe). ``run_after`` rules: a due/overdue interval loop or a
never-run chain head fires now; a future interval/daily slot fires at that slot; a
cadence-less (every-tick) loop polls on a 60 s floor.

Step 3 — admission check: the unified enabled+due+reachable verdict
(:func:`teatree.loops.loop_table.admitted_loop_names`). A held/disabled/not-due loop
is a free no-op; its successor is refined to a polling floor so it never busy-spins.

Step 4 — deadlined subprocess tick: the tick runs as its OWN process group
subprocess (``python -m teatree loops_tick --loop <name>``) with a hard deadline
``max(300 s, 3 x cadence)``; on expiry the whole group is killed, so a hung tick
occupies one executor slot for at most the deadline and every other loop keeps firing.

Step 5 — post-tick refinement: after the tick's CAS bumps ``Loop.last_run_at``, the
successor's ``run_after`` is recomputed from the fresh anchor and pushed out to the
precise next slot.

Idempotency is inherited: at-least-once delivery from django-tasks means a
``loop_timer`` can fire twice; the per-loop tick's ``mark_run_if_unchanged`` CAS
makes the redelivered run a no-op, and step 1's self-dedup collapses redundant
successors, so a double delivery never doubles the chain.
"""

import datetime as dt
import logging
import os
import signal
import sys
from typing import TYPE_CHECKING, TypedDict

from django.tasks import task
from django.utils import timezone

from teatree.utils.run import Popen, TimeoutExpired, spawn_session_leader

if TYPE_CHECKING:
    from django_tasks_db.models import DBTaskResult

    from teatree.core.models import Loop

logger = logging.getLogger(__name__)


class TickOutcome(TypedDict):
    """The result of one deadlined subprocess tick."""

    timed_out: bool
    returncode: int | None


class TimerResult(TypedDict, total=False):
    """One ``loop_timer`` fire's outcome — the branch taken plus any tick result."""

    loop: str
    action: str
    timed_out: bool
    returncode: int | None


#: The dedicated django-tasks queue every loop timer chain rides. The worker pins
#: half its executor threads here so a reactive timer never blocks behind a heavy
#: ``default``-queue FSM/headless job. Mirrors the ``TASKS["default"]["QUEUES"]``
#: allowlist in ``teatree.settings`` (parity-tested).
LOOPS_QUEUE = "loops"

#: A cadence-less (every-tick) loop has no interval, so its successor polls on this
#: floor rather than busy-spinning.
CADENCE_LESS_POLL_FLOOR_SECONDS = 60

#: A held/disabled/not-yet-due loop's successor is floored here so an idle chain
#: polls at a sane cadence instead of re-firing immediately.
IDLE_POLL_FLOOR_SECONDS = 60

#: The tick subprocess deadline is ``max(MIN_TICK_DEADLINE_SECONDS, 3 x cadence)``.
MIN_TICK_DEADLINE_SECONDS = 300.0
DEADLINE_CADENCE_MULTIPLIER = 3


def _loop_timer_path() -> str:
    """The dotted ``task_path`` django-tasks stores for :func:`loop_timer` rows."""
    return loop_timer.module_path


def _timers_for(name: str, *, status: str) -> "list[DBTaskResult]":
    """The ``loop_timer`` DBTaskResult rows for *name* in *status*.

    The DB pre-filters on ``task_path`` + ``status`` (small — at most a few timer
    rows per loop) and the loop-name match is done in Python against the stored
    ``args`` list, so the query stays backend-agnostic (no JSONField array-index
    lookup) and still exact.
    """
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415

    rows = DBTaskResult.objects.filter(task_path=_loop_timer_path(), status=status)
    return [row for row in rows if row.args_kwargs.get("args") == [name]]


def pending_loop_timers(name: str) -> "list[DBTaskResult]":
    """READY (queued, not yet claimed) ``loop_timer`` rows for *name*."""
    from django_tasks.base import TaskResultStatus  # noqa: PLC0415

    return _timers_for(name, status=TaskResultStatus.READY)


def running_loop_timers(name: str) -> "list[DBTaskResult]":
    """RUNNING (claimed, executing) ``loop_timer`` rows for *name*."""
    from django_tasks.base import TaskResultStatus  # noqa: PLC0415

    return _timers_for(name, status=TaskResultStatus.RUNNING)


def enqueue_loop_timer(name: str, *, run_after: dt.datetime) -> None:
    """Queue one ``loop_timer(name)`` timer on the ``loops`` queue at *run_after*."""
    loop_timer.using(run_after=run_after).enqueue(name)


def refine_successor(name: str, *, run_after: dt.datetime) -> None:
    """Push the pending successor timer(s) for *name* out to *run_after*.

    A direct ``run_after`` update on the READY rows — the post-tick cadence
    refinement (and the idle-poll floor for a skipped loop). A no-op when no
    successor is pending (the successor-first enqueue guarantees one under normal
    flow).
    """
    from django_tasks.base import TaskResultStatus  # noqa: PLC0415
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415

    ids = [row.id for row in _timers_for(name, status=TaskResultStatus.READY)]
    if ids:
        DBTaskResult.objects.filter(id__in=ids).update(run_after=run_after)


def compute_successor_run_after(row: "Loop", now: dt.datetime) -> dt.datetime:
    """When *row*'s next timer should fire — the conservative, crash-safe cadence.

    A future interval anchor or daily slot fires at that slot; a due/overdue
    interval loop and a never-run interval chain head both fire now; a cadence-less
    (no interval, no daily) loop polls on the 60 s floor so it never busy-spins.
    """
    nxt = row.next_run_at()
    if nxt is not None and nxt > now:
        return nxt
    if row.delay_seconds is None and row.daily_at is None:
        return now + dt.timedelta(seconds=CADENCE_LESS_POLL_FLOOR_SECONDS)
    return now


def _idle_successor_run_after(row: "Loop", now: dt.datetime) -> dt.datetime:
    """A skipped loop's successor — the cadence, floored so an idle chain polls sanely.

    A not-yet-due loop keeps its future slot; a held/disabled-but-otherwise-due
    loop (whose cadence anchor did NOT move because the tick was skipped) is floored
    to ``now + 60 s`` so it polls rather than re-firing immediately.
    """
    return max(compute_successor_run_after(row, now), now + dt.timedelta(seconds=IDLE_POLL_FLOOR_SECONDS))


def compute_tick_deadline(row: "Loop") -> float:
    """The hard subprocess-tick deadline: ``max(300 s, 3 x cadence)``."""
    cadence = row.delay_seconds or 0
    return max(MIN_TICK_DEADLINE_SECONDS, DEADLINE_CADENCE_MULTIPLIER * float(cadence))


def loop_admitted(name: str, now: dt.datetime) -> bool:
    """Whether *name* passes the unified enabled+due+reachable verdict right now.

    Reuses :func:`teatree.loops.loop_table.admitted_loop_names` scoped to the one
    loop, so the timer chain's admission can never drift from the tick's.
    """
    from teatree.loops.loop_table import admitted_loop_names  # noqa: PLC0415

    return name in admitted_loop_names(now, only=name)


def _tick_argv(name: str) -> list[str]:
    """The subprocess argv for one per-loop tick — ``python -m teatree loops_tick --loop <name>``."""
    return [sys.executable, "-m", "teatree", "loops_tick", "--loop", name]


def run_deadlined_tick(name: str, *, deadline: float) -> TickOutcome:
    """Run one per-loop tick as a deadlined subprocess in its OWN process group.

    ``python -m teatree loops_tick --loop <name>`` is spawned with
    ``start_new_session=True`` so it leads a fresh process group; on deadline expiry
    the WHOLE group is ``SIGKILL``-ed (the tick plus any grandchildren it spawned),
    so a hung tick can never outlive its deadline or strand children. Standard over
    clever: a subprocess via ``python -m teatree`` isolates a crash/hang from the
    worker executor thread and gives an OS-level kill boundary an in-process
    ``call_command`` cannot.
    """
    proc = spawn_session_leader(_tick_argv(name))
    try:
        returncode = proc.wait(timeout=deadline)
    except TimeoutExpired:
        _kill_process_group(proc)
        logger.warning("loop_timer %r tick exceeded its %.0fs deadline — killed the process group", name, deadline)
        return {"timed_out": True, "returncode": None}
    return {"timed_out": False, "returncode": returncode}


def _kill_process_group(proc: Popen[str]) -> None:
    """SIGKILL the subprocess's whole process group, tolerating an already-dead child."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=10)
    except TimeoutExpired:
        logger.exception("loop tick process group for pid %s did not die after SIGKILL", proc.pid)


@task(queue_name=LOOPS_QUEUE)
def loop_timer(name: str) -> TimerResult:
    """One self-rescheduling loop-timer fire — the five-step tick body (#1796).

    See the module docstring for the step-by-step contract. The running row is
    already RUNNING (the worker claimed it before calling), so a READY row for the
    same loop is unambiguously a duplicate successor — the self-dedup in step 1 does
    not need this row's own id.
    """
    from teatree.core.models import Loop  # noqa: PLC0415

    now = timezone.now()

    # (1) self-dedup — another queued timer already carries the chain.
    if pending_loop_timers(name):
        return {"loop": name, "action": "deduped"}

    row = Loop.objects.filter(name=name).first()
    if row is None:
        # The loop was deleted; do not re-chain (the reconciler prunes stragglers).
        return {"loop": name, "action": "unknown"}

    # (2) successor-first re-enqueue — crash-safe, BEFORE any tick work.
    enqueue_loop_timer(name, run_after=compute_successor_run_after(row, now))

    # (3) admission — a held/disabled/not-due loop is a free no-op.
    if not loop_admitted(name, now):
        refine_successor(name, run_after=_idle_successor_run_after(row, now))
        return {"loop": name, "action": "skipped"}

    # (4) deadlined subprocess tick in its own process group.
    outcome = run_deadlined_tick(name, deadline=compute_tick_deadline(row))

    # (5) post-tick refinement from the fresh CAS anchor.
    fresh = Loop.objects.filter(name=name).first()
    if fresh is not None:
        refine_successor(name, run_after=compute_successor_run_after(fresh, timezone.now()))

    return {
        "loop": name,
        "action": "ticked",
        "timed_out": outcome["timed_out"],
        "returncode": outcome["returncode"],
    }
