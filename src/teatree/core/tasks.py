import logging
from typing import TypedDict

from django.db import transaction
from django.tasks import task

from teatree.config import worktree_root
from teatree.core.landscape_gather import run_landscape
from teatree.core.models import LandscapeArtifact, Task, Ticket
from teatree.core.models.external_delivery import under_external_delivery
from teatree.core.models.trivial_plan_skip import is_trivial_plan_skip
from teatree.core.runners import RetroExecutor, ShipExecutor, WorktreeProvisioner, WorktreeTeardown

logger = logging.getLogger(__name__)


def _persist_intake_landscape(ticket: Ticket) -> None:
    """Bake the intake landscape survey into a durable artifact (#2541).

    Run after the worktrees materialise and before the planner is scheduled, so
    the planner consumes the survey the intake FSM step produced instead of
    re-deriving it. Best-effort context, never a gate: any gather failure (a
    forge outage, a corrupt clone) or an empty survey degrades to a log line —
    it must NEVER abort provisioning or block the planner (fail-open, mirroring
    the landscape module's own degradation doctrine). A survey with only
    warnings is still a non-empty dict, so it is persisted; a gather that raises
    leaves no artifact.
    """
    try:
        survey = run_landscape(worktree_root())
    except Exception:
        logger.warning("Intake landscape gather failed for ticket %s; skipping artifact", ticket.pk, exc_info=True)
        return
    try:
        LandscapeArtifact.record(ticket=ticket, survey=survey, recorded_by="t3:intake")
    except ValueError:
        logger.info("Intake landscape survey for ticket %s was empty; no artifact recorded", ticket.pk)


class TransitionResult(TypedDict, total=False):
    ticket_id: int
    ok: bool
    skipped: bool
    state: str
    detail: str


@task()
def execute_headless_task(task_id: int, phase: str) -> dict[str, object]:
    import traceback  # noqa: PLC0415

    from teatree.core.headless_dispatch import loop_dispatch_refusal  # noqa: PLC0415
    from teatree.core.overlay_loader import get_overlay_for_ticket  # noqa: PLC0415

    task_obj = Task.objects.get(pk=task_id)

    # Poison-pill guard (souliane/teatree#1959): a task whose ticket names a
    # non-empty overlay that no longer resolves crashes ``get_overlay_for_ticket``
    # on every drain. Fail it permanently here — a recorded FAILED attempt the
    # operator can inspect — instead of raising an exception that re-fires next
    # tick.
    if not task_obj.ticket.has_dispatchable_overlay():
        reason = f"unknown overlay {task_obj.ticket.overlay!r}: ticket {task_obj.ticket_id} cannot be dispatched"
        logger.warning("Task %s: %s", task_obj.pk, reason)
        if task_obj.status == Task.Status.PENDING:
            task_obj.claim(claimed_by="unknown-overlay-guard")
        task_obj.complete_with_attempt(exit_code=1, error=reason, result={"unknown_overlay": reason})
        return {"exit_code": 1, "unknown_overlay": reason}

    # Fail-closed billing guard: a loop-dispatched phase task (one whose
    # (role, phase) has a registered phase agent) must run INTERACTIVE in the
    # in-session ``/loop`` slot, never as a metered detached headless-SDK run.
    # The predicate lives in ONE shared helper both headless entry
    # points consult (``loop_dispatch_refusal``), so the ``work-next-sdk`` CLI
    # path cannot drift from this seam (souliane/teatree#1375). Refuse here and
    # record a ``routing_error`` instead of shelling out — closing the seam
    # where a stray enqueue (a re-enqueue, a queue drainer, a manual
    # ``enqueue``) would silently meter the loop's phase work.
    routing_refusal = loop_dispatch_refusal(task_obj)
    if routing_refusal is not None:
        logger.warning("Task %s: %s", task_obj.pk, routing_refusal)
        if task_obj.status == Task.Status.PENDING:
            task_obj.claim(claimed_by="headless-routing-guard")
        task_obj.complete_with_attempt(exit_code=1, error=routing_refusal, result={"routing_error": routing_refusal})
        return {"exit_code": 1, "routing_error": routing_refusal}

    # Claim here (when the worker actually starts) instead of at enqueue time
    if task_obj.status == Task.Status.PENDING:
        task_obj.claim(claimed_by="headless-worker")
    try:
        from teatree.core.headless_dispatch import get_headless_runner  # noqa: PLC0415

        overlay = get_overlay_for_ticket(task_obj.ticket)
        attempt = get_headless_runner()(
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

    Tasks the in-session ``/loop`` owns (``runs_in_session`` — a loop-dispatched
    phase pair under ``agent_runtime=interactive``) are skipped, so draining them
    here never double-runs work the loop is dispatching (the same guard the
    ``_auto_enqueue_headless_task`` post_save applies). Under a headless
    ``agent_runtime`` those phase tasks are headless and ``runs_in_session`` is
    ``False``, so they drain like any other headless work.

    A task whose ticket names a non-empty unknown overlay is failed permanently
    rather than re-enqueued (souliane/teatree#1959): re-enqueuing it would crash
    ``execute_headless_task`` on every tick forever — the poison pill this drain
    must not keep feeding. A blank overlay is the ambient single-overlay default
    and stays dispatchable.
    """
    from teatree.core.headless_dispatch import runs_in_session  # noqa: PLC0415

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
        if runs_in_session(role=task_obj.ticket.role, phase=task_obj.phase):
            continue
        if not task_obj.ticket.has_dispatchable_overlay():
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
def sync_followup() -> dict[str, int | list[str] | list[dict[str, int | str]]]:
    from teatree.core.sync import sync_followup as _sync  # noqa: PLC0415

    result = _sync()
    return {
        "prs_found": result.prs_found,
        "tickets_created": result.tickets_created,
        "tickets_updated": result.tickets_updated,
        "errors": result.errors,
        "conflicted_mrs": [c.to_dict() for c in result.conflicted_mrs],
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
    """Tear down worktrees for a MERGED ticket via the analyze-then-wipe reaper.

    Idempotency: the worker takes a row lock and re-checks state before running.
    At-least-once delivery from django-tasks means this can fire more than once
    for the same transition — a lost update or a redelivered job must be safe.

    Teardown is best-effort: per-worktree errors are reported in the result
    detail but do not advance the ticket. The ticket stays in MERGED until
    the operator either fixes the underlying issue and re-enqueues, or moves
    on with ``retrospect()`` once the residual state is acceptable.

    There is no force-bypass (CORRECTION 1): :class:`WorktreeTeardown` routes every
    worktree through the analyze-before-wipe reaper, which proves each unpushed
    commit and uncommitted change redundant before wiping. A squash-merge that
    landed a new SHA and deleted the source ref is proven redundant by patch-id and
    wiped; a branch with genuinely-unsynced work (an async ship that never drained,
    #707/#708) is KEPT and surfaced, never force-destroyed.
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

    On success, the runner has materialised every git worktree; we then bake the
    intake landscape survey into a durable ``LandscapeArtifact`` (#2541) so the
    planner consumes the survey the intake FSM step produced rather than
    re-deriving it (best-effort — a gather failure never blocks the FSM), and call
    ``schedule_planning()`` so the FSM proceeds toward CODED — unless the unit
    is under active external delivery (#2104), in which case the auto-planner is
    skipped (a hand-dispatched delivery agent implements directly with no
    planning phase, so the planner would be orphaned), or the unit carries the
    lightweight trivial-skip marker (a trivial mechanical edit the operator
    explicitly opted out of planning, mirroring the external-delivery skip). The
    loop's own autonomous FSM never stamps either marker, so its flow is
    unchanged.
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

        _persist_intake_landscape(ticket)

        if under_external_delivery(ticket):
            logger.info("Ticket %s under external delivery; skipping auto-planner (#2104)", ticket_id)
        elif is_trivial_plan_skip(ticket):
            logger.info("Ticket %s marked trivial; skipping auto-planner (plan-gate carve-out)", ticket_id)
        else:
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
