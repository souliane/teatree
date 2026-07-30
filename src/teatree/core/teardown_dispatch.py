"""The teardown enqueue seam — one idempotent front door to ``execute_teardown``.

Split out of :mod:`teatree.core.tasks` (module-health public-function budget),
mirroring the ``managers_phase_cadence`` concern split. ``tasks`` owns the task
BODY — what a teardown DOES; this module owns the decision to queue one, which is
where the duplication was.

Teardown is safe to repeat, and that is precisely why it was repeated: a caller
re-ran it at tick cadence, converging the ticket's STATE while every repetition
left a durable ``DBTaskResult`` row for work that was already queued. A write
meaning "this work is scheduled" has to be idempotent in its SIDE EFFECTS, not
only in the state it converges to (#3879), so both callers — the FSM's
terminal-state ``on_commit`` hook and the operator drain — go through
:func:`enqueue_teardown_once` rather than carrying a guard each.
"""

import logging

from teatree.core.models import Ticket
from teatree.core.tasks import execute_teardown
from teatree.core.worktree.worktree_done import _DONE_TICKET_STATES

logger = logging.getLogger(__name__)

#: Bound at import from the real task, so the queue read still finds the rows a
#: test's patched ``execute_teardown`` stand-in would not know its own path for.
_TEARDOWN_TASK_PATH = execute_teardown.module_path


def teardown_outstanding_for(ticket_id: int) -> bool:
    """True iff a teardown job for *ticket_id* is queued (READY) or in flight (RUNNING).

    Reads the job queue directly — the queue IS the record of "this teardown is
    already scheduled", so no parallel marker is introduced beside it. Mirrors
    :func:`teatree.loops.timer_chains._live_loop_timers`, the same READY-or-RUNNING
    self-dedup the loop-timer chains use.

    A FINISHED job (SUCCESSFUL or FAILED) is deliberately NOT outstanding. The
    reaper refuses rather than raises when it leaves a worktree standing (unsynced
    work is KEPT, #706/#707), so a SUCCESSFUL job routinely means "ran, and the
    worktree is still there" — treating it as covering the ticket forever would
    turn this guard into a permanent block on legitimate re-attempts.
    """
    from django_tasks.base import TaskResultStatus  # noqa: PLC0415 — deferred: heavy/optional dep at call site
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415 — deferred: heavy/optional dep at call site

    return DBTaskResult.objects.filter(
        task_path=_TEARDOWN_TASK_PATH,
        status__in=[TaskResultStatus.READY, TaskResultStatus.RUNNING],
        args_kwargs__args=[int(ticket_id)],
    ).exists()


def enqueue_teardown_once(ticket_id: int) -> bool:
    """Queue ``execute_teardown`` for *ticket_id* unless one is already outstanding.

    Returns whether this call minted a job. The executor is reached through the
    ``tasks`` module attribute rather than a bound import, so the FSM receivers'
    patch of ``tasks.execute_teardown`` stays visible through this seam.

    Deliberately NOT deduplicated against a finished job — see
    :func:`teardown_outstanding_for`. A genuine second attempt after a reaper
    refusal or a failed run still queues.
    """
    from teatree.core import tasks as tasks_mod  # noqa: PLC0415 — deferred: call-time attribute lookup

    if teardown_outstanding_for(ticket_id):
        logger.debug("teardown already outstanding for ticket %s — not queuing another", ticket_id)
        return False
    tasks_mod.execute_teardown.enqueue(int(ticket_id))
    return True


def enqueue_teardown_for_terminal_tickets() -> list[int]:
    """One-shot backlog drain: enqueue teardown for every terminal ticket still holding worktrees.

    The operational catch-up for tickets whose worktrees outlived their terminal
    state. Safe to re-run: ``execute_teardown`` re-checks state, the reaper keeps
    any unsynced work, and the enqueue itself deduplicates against an outstanding
    job, so repeating the drain does not repeat the queue rows. NOT invoked
    automatically; an operator calls it explicitly to drain the pile-up.

    Returns the ticket pks this call actually queued — a ticket whose teardown was
    already outstanding is covered but not re-queued, so it is absent.
    """
    ticket_ids = list(
        Ticket.objects.filter(state__in=_DONE_TICKET_STATES, worktrees__isnull=False)
        .distinct()
        .values_list("pk", flat=True)
    )
    return [int(ticket_id) for ticket_id in ticket_ids if enqueue_teardown_once(int(ticket_id))]
