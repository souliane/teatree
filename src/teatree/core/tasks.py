import logging
from typing import TypedDict

from django.db import transaction
from django.tasks import task

from teatree.core.models import Task, Ticket
from teatree.core.runners import RetroExecutor, ShipExecutor, WorktreeProvisioner, WorktreeTeardown

logger = logging.getLogger(__name__)


class TransitionResult(TypedDict, total=False):
    ticket_id: int
    ok: bool
    skipped: bool
    state: str
    detail: str


@task()
def execute_headless_task(task_id: int, phase: str) -> dict[str, object]:
    import traceback  # noqa: PLC0415

    from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415

    task_obj = Task.objects.get(pk=task_id)
    # Claim here (when the worker actually starts) instead of at enqueue time
    if task_obj.status == Task.Status.PENDING:
        task_obj.claim(claimed_by="headless-worker")
    try:
        from teatree.agents.headless import run_headless  # noqa: PLC0415

        overlay = get_overlay()
        attempt = run_headless(
            task_obj,
            phase=phase,
            overlay_skill_metadata=overlay.metadata.get_skill_metadata(),
        )
    except Exception:
        task_obj.complete_with_attempt(exit_code=1, error=traceback.format_exc())
        raise
    else:
        return {"attempt_id": attempt.pk, "exit_code": attempt.exit_code, "result": attempt.result}


@task()
def drain_headless_queue() -> dict[str, list[int]]:
    """Auto-enqueue pending headless tasks for execution."""
    pending = Task.objects.filter(
        execution_target=Task.ExecutionTarget.HEADLESS,
        status=Task.Status.PENDING,
    ).values_list("pk", "phase")
    enqueued: list[int] = []
    for task_id, phase in pending:
        execute_headless_task.enqueue(task_id, phase)
        enqueued.append(task_id)
    return {"enqueued": enqueued}


@task()
def sync_followup() -> dict[str, int | list[str]]:
    from teatree.core.sync import sync_followup as _sync  # noqa: PLC0415

    result = _sync()
    return {
        "mrs_found": result.mrs_found,
        "tickets_created": result.tickets_created,
        "tickets_updated": result.tickets_updated,
        "errors": result.errors,
    }


@task()
def refresh_followup_snapshot() -> dict[str, int]:
    return {
        "tickets": Ticket.objects.count(),
        "tasks": Task.objects.count(),
        "open_tasks": Task.objects.exclude(status=Task.Status.COMPLETED).count(),
    }


@task()
def execute_retrospect(ticket_id: int) -> TransitionResult:
    """Run retrospection I/O for a ticket in the RETROSPECTED state.

    Idempotency: the worker takes a row lock and re-checks state before running.
    At-least-once delivery from django-tasks means this can fire more than once
    for the same transition — a lost update or a redelivered job must be safe.

    On success, advances ``RETROSPECTED → DELIVERED`` via ``mark_delivered()``.
    """
    with transaction.atomic():
        ticket = Ticket.objects.select_for_update().get(pk=ticket_id)
        if ticket.state != Ticket.State.RETROSPECTED:
            logger.info(
                "execute_retrospect skipped for ticket %s: state=%s (not RETROSPECTED)",
                ticket_id,
                ticket.state,
            )
            return {"ticket_id": ticket_id, "skipped": True, "state": str(ticket.state)}

        result = RetroExecutor(ticket).run()
        if not result.ok:
            logger.warning("Retro failed for ticket %s: %s", ticket_id, result.detail)
            return {"ticket_id": ticket_id, "ok": False, "detail": result.detail}

        ticket.mark_delivered()
        ticket.save()

    return {"ticket_id": ticket_id, "ok": True, "detail": result.detail}


@task()
def execute_teardown(ticket_id: int) -> TransitionResult:
    """Tear down worktrees for a MERGED ticket.

    Idempotency: the worker takes a row lock and re-checks state before running.
    At-least-once delivery from django-tasks means this can fire more than once
    for the same transition — a lost update or a redelivered job must be safe.

    Teardown is best-effort: per-worktree errors are reported in the result
    detail but do not advance the ticket. The ticket stays in MERGED until
    the operator either fixes the underlying issue and re-enqueues, or moves
    on with ``retrospect()`` once the residual state is acceptable.
    """
    with transaction.atomic():
        ticket = Ticket.objects.select_for_update().get(pk=ticket_id)
        if ticket.state != Ticket.State.MERGED:
            logger.info(
                "execute_teardown skipped for ticket %s: state=%s (not MERGED)",
                ticket_id,
                ticket.state,
            )
            return {"ticket_id": ticket_id, "skipped": True, "state": str(ticket.state)}

        result = WorktreeTeardown(ticket).run()
        if not result.ok:
            logger.warning("Teardown reported errors for ticket %s: %s", ticket_id, result.detail)
            return {"ticket_id": ticket_id, "ok": False, "detail": result.detail}

    return {"ticket_id": ticket_id, "ok": True, "detail": result.detail}


@task()
def execute_provision(ticket_id: int) -> TransitionResult:
    """Provision worktrees for a STARTED ticket and schedule the coding task.

    Idempotency: the worker takes a row lock and re-checks state before running.
    At-least-once delivery from django-tasks means this can fire more than once
    for the same transition — a lost update or a redelivered job must be safe.

    On success, the runner has materialised every git worktree; we then call
    ``schedule_coding()`` so the FSM proceeds toward CODED.
    """
    with transaction.atomic():
        ticket = Ticket.objects.select_for_update().get(pk=ticket_id)
        if ticket.state != Ticket.State.STARTED:
            logger.info(
                "execute_provision skipped for ticket %s: state=%s (not STARTED)",
                ticket_id,
                ticket.state,
            )
            return {"ticket_id": ticket_id, "skipped": True, "state": str(ticket.state)}

        result = WorktreeProvisioner(ticket).run()
        if not result.ok:
            logger.warning("Provision failed for ticket %s: %s", ticket_id, result.detail)
            return {"ticket_id": ticket_id, "ok": False, "detail": result.detail}

        ticket.schedule_coding()

    return {"ticket_id": ticket_id, "ok": True, "detail": result.detail}


@task()
def execute_ship(ticket_id: int) -> TransitionResult:
    """Push the worktree branch and open the merge request for a SHIPPED ticket.

    Idempotency: the worker takes a row lock and re-checks state before running.
    At-least-once delivery from django-tasks means this can fire more than once
    for the same transition — a lost update or a redelivered job must be safe.

    On success, advances ``SHIPPED → IN_REVIEW`` via ``request_review()``.
    """
    with transaction.atomic():
        ticket = Ticket.objects.select_for_update().get(pk=ticket_id)
        if ticket.state != Ticket.State.SHIPPED:
            logger.info(
                "execute_ship skipped for ticket %s: state=%s (not SHIPPED)",
                ticket_id,
                ticket.state,
            )
            return {"ticket_id": ticket_id, "skipped": True, "state": str(ticket.state)}

        result = ShipExecutor(ticket).run()
        if not result.ok:
            logger.warning("Ship failed for ticket %s: %s", ticket_id, result.detail)
            return {"ticket_id": ticket_id, "ok": False, "detail": result.detail}

        ticket.request_review()
        ticket.save()

    return {"ticket_id": ticket_id, "ok": True, "detail": result.detail}
