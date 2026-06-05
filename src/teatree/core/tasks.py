import logging
from typing import TypedDict

from django.conf import settings
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

    from teatree.core.overlay_loader import get_overlay_for_ticket, resolve_overlay_name  # noqa: PLC0415

    task_obj = Task.objects.get(pk=task_id)

    # Poison-pill guard (souliane/teatree#1959): a task whose ticket names a
    # non-empty overlay that no longer resolves crashes ``get_overlay_for_ticket``
    # on every drain. Fail it permanently here — a recorded FAILED attempt the
    # operator can inspect — instead of raising an exception that re-fires next
    # tick. A blank overlay is the ambient single-overlay default and stays
    # dispatchable (``get_overlay(None)`` resolves it).
    if task_obj.ticket.overlay and resolve_overlay_name(task_obj.ticket.overlay) is None:
        reason = f"unknown overlay {task_obj.ticket.overlay!r}: ticket {task_obj.ticket_id} cannot be dispatched"
        logger.warning("Task %s: %s", task_obj.pk, reason)
        if task_obj.status == Task.Status.PENDING:
            task_obj.claim(claimed_by="unknown-overlay-guard")
        task_obj.complete_with_attempt(exit_code=1, error=reason, result={"unknown_overlay": reason})
        return {"exit_code": 1, "unknown_overlay": reason}

    # Fail-closed billing guard: a loop-dispatched phase task (one whose
    # (role, phase) has a registered phase agent) must run INTERACTIVE in the
    # in-session ``/loop`` slot, never as a metered detached ``claude -p``
    # subprocess. Unless the single ``LOOP_ALLOW_HEADLESS_DISPATCH`` toggle is
    # explicitly enabled, refuse here and record a ``routing_error`` instead
    # of shelling out — closing the seam where a stray enqueue (a re-enqueue,
    # a queue drainer, a manual ``enqueue``) would silently meter the loop's
    # phase work.
    if not getattr(settings, "LOOP_ALLOW_HEADLESS_DISPATCH", False) and Task.loop_dispatched(
        role=task_obj.ticket.role,
        phase=task_obj.phase,
    ):
        reason = (
            f"refused headless dispatch for loop-dispatched phase "
            f"(role={task_obj.ticket.role!r}, phase={task_obj.phase!r}): "
            "this task must run INTERACTIVE via the /loop slot "
            "(set LOOP_ALLOW_HEADLESS_DISPATCH=True to override)"
        )
        logger.warning("Task %s: %s", task_obj.pk, reason)
        if task_obj.status == Task.Status.PENDING:
            task_obj.claim(claimed_by="headless-routing-guard")
        task_obj.complete_with_attempt(exit_code=1, error=reason, result={"routing_error": reason})
        return {"exit_code": 1, "routing_error": reason}

    # Claim here (when the worker actually starts) instead of at enqueue time
    if task_obj.status == Task.Status.PENDING:
        task_obj.claim(claimed_by="headless-worker")
    try:
        from teatree.agents.headless import run_headless  # noqa: PLC0415

        overlay = get_overlay_for_ticket(task_obj.ticket)
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
    """Auto-enqueue pending headless tasks for execution (safety net), failing poison rows.

    Loop-dispatched phase tasks (those whose ``(ticket.role, phase)`` has a
    registered phase agent) are skipped — the loop is their sole dispatcher,
    so draining them here would double-run them (the same guard the
    ``_auto_enqueue_headless_task`` post_save applies). Only genuinely
    headless tasks with no registered phase agent are drained.

    A task whose ticket names a non-empty unknown overlay is failed permanently
    rather than re-enqueued (souliane/teatree#1959): re-enqueuing it would crash
    ``execute_headless_task`` on every tick forever — the poison pill this drain
    must not keep feeding. A blank overlay is the ambient single-overlay default
    and stays dispatchable.
    """
    from teatree.core.overlay_loader import resolve_overlay_name  # noqa: PLC0415
    from teatree.core.phases import subagent_for_phase  # noqa: PLC0415

    pending = (
        Task.objects.filter(
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.PENDING,
        )
        .select_related("ticket")
        .only("pk", "phase", "ticket__role", "ticket__overlay")
    )
    enqueued: list[int] = []
    failed_unknown_overlay: list[int] = []
    for task_obj in pending:
        if subagent_for_phase(task_obj.ticket.role, task_obj.phase):
            continue
        if task_obj.ticket.overlay and resolve_overlay_name(task_obj.ticket.overlay) is None:
            reason = f"unknown overlay {task_obj.ticket.overlay!r}: ticket {task_obj.ticket_id} cannot be dispatched"
            logger.warning("Drain: failing task %s permanently — %s", task_obj.pk, reason)
            task_obj.claim(claimed_by="unknown-overlay-guard")
            task_obj.complete_with_attempt(exit_code=1, error=reason, result={"unknown_overlay": reason})
            failed_unknown_overlay.append(task_obj.pk)
            continue
        execute_headless_task.enqueue(task_obj.pk, task_obj.phase)
        enqueued.append(task_obj.pk)
    return {"enqueued": enqueued, "failed_unknown_overlay": failed_unknown_overlay}


@task()
def sync_followup() -> dict[str, int | list[str]]:
    from teatree.core.sync import sync_followup as _sync  # noqa: PLC0415

    result = _sync()
    return {
        "prs_found": result.prs_found,
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
    """Provision worktrees for a STARTED ticket and schedule the planning task.

    Idempotency: the worker takes a row lock and re-checks state before running.
    At-least-once delivery from django-tasks means this can fire more than once
    for the same transition — a lost update or a redelivered job must be safe.

    On success, the runner has materialised every git worktree; we then call
    ``schedule_planning()`` so the FSM proceeds toward CODED.
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

        ticket.schedule_planning()

    return {"ticket_id": ticket_id, "ok": True, "detail": result.detail}


@task()
def execute_ship(ticket_id: int) -> TransitionResult:
    """Push the worktree branch and open the pull request for a SHIPPED ticket.

    Idempotency: the worker takes a row lock and re-checks state before running.
    At-least-once delivery from django-tasks means this can fire more than once
    for the same transition — a lost update or a redelivered job must be safe.

    On success, advances ``SHIPPED → IN_REVIEW`` via ``request_review()``.

    ``ShipExecutor.run()`` runs OUTSIDE the FSM-advance transaction (#1522):
    it calls ``host.create_pr()``, whose live forge PR is an external side
    effect no rollback can undo. Run as a top-level operation, the executor's
    own ``merge_extra`` records the PR url in its own committed transaction
    the instant ``create_pr`` returns, so a later rollback of the FSM advance
    cannot strand the PR. A redelivered job then finds the recorded url and
    adopts it (``ShipExecutor._recorded_url_for_branch``) instead of
    re-calling ``create_pr`` and hitting a 409.
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

    with transaction.atomic():
        ticket = Ticket.objects.select_for_update().get(pk=ticket_id)
        if ticket.state != Ticket.State.SHIPPED:
            logger.info(
                "execute_ship FSM advance skipped for ticket %s: state=%s (PR already recorded, not SHIPPED)",
                ticket_id,
                ticket.state,
            )
            return {"ticket_id": ticket_id, "ok": True, "detail": result.detail}
        ticket.request_review()
        ticket.save()

    return {"ticket_id": ticket_id, "ok": True, "detail": result.detail}
