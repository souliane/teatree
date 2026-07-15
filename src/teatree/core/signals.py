import logging

from django.db import transaction
from django.db.models.signals import post_save
from django_fsm.signals import post_transition

from teatree.core.headless_dispatch import runs_in_session
from teatree.core.models.implemented_issue_marker import ImplementedIssueMarker
from teatree.core.models.pull_request import PullRequest
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket
from teatree.core.models.worktree import Worktree
from teatree.core.on_behalf_gate_recorded import OnBehalfPostBlockedError, require_on_behalf_approval
from teatree.core.on_behalf_post_receipt import notify_user_on_behalf_post
from teatree.core.reaction_dispatch import get_reaction_publisher

logger = logging.getLogger(__name__)

# Transition name -> the ``@task`` worker its enqueue used to live in the FSM
# body (#2385: the body's intra-core up-edge into ``teatree.core.tasks`` is
# severed; the enqueue is keyed here, looked up + enqueued after commit by the
# post_transition receiver so the state change and the queued work still land
# atomically). ``reconcile_merged`` mirrors ``mark_merged`` — both tear down.
_TICKET_TRANSITION_TASKS: dict[str, str] = {
    "start": "execute_provision",
    "ship": "execute_ship",
    "mark_merged": "execute_teardown",
    "reconcile_merged": "execute_teardown",
    "retrospect": "execute_retrospect",
}

_WORKTREE_TRANSITION_TASKS: dict[str, str] = {
    "provision": "execute_worktree_provision",
    "start_services": "execute_worktree_start",
    "verify": "execute_worktree_verify",
    "stop_services": "execute_worktree_stop",
}

# A ticket reaching one of these frees its issue-implementer marker from the
# single-ticket in-flight budget — the release-on-completion the lifecycle lacked.
_MARKER_RELEASE_TARGET_STATES: frozenset[str] = frozenset(
    {Ticket.State.MERGED, Ticket.State.DELIVERED, Ticket.State.IGNORED}
)


def _log_ticket_transition(
    sender: type,  # noqa: ARG001 — Django signal receiver signature requires sender even when unused
    instance: Ticket,
    name: str,
    source: str,
    target: str,
    **_kwargs: object,
) -> None:
    from teatree.core.models.transition import TicketTransition  # noqa: PLC0415 — deferred: ORM/app-registry

    try:
        session = instance.sessions.order_by("-started_at").first()  # ty: ignore[unresolved-attribute]
        TicketTransition.objects.create(
            ticket=instance,
            session=session,
            from_state=source,
            to_state=target,
            triggered_by=name,
        )
    except Exception:
        logger.exception("Failed to record TicketTransition audit for ticket %s transition %s", instance.pk, name)


def _add_slack_reactions_on_transition(
    instance: Ticket,
    name: str,
    **_kwargs: object,
) -> None:
    """Post a Slack emoji reaction on the PR review message for this transition.

    The reaction is on a colleague-facing surface (the review-request
    message the user posted to reviewers), so it is gated by the
    recorded-approval on-behalf path. Gate ON + recorded approval scoped
    to ``(ticket.url, "transition_reaction:<name>")`` → reaction posts;
    gate ON + no approval → skip without posting; gate OFF → post. The
    FSM transition itself is never blocked.
    """
    target = f"ticket:{instance.pk}"
    try:
        reacted = require_on_behalf_approval(
            target=target,
            action=f"transition_reaction:{name}",
            publish=lambda: get_reaction_publisher().add_reactions_for_transition(instance, name),
        )
    except OnBehalfPostBlockedError as blocked:
        logger.info("Transition reaction for ticket %s (%s) gated: %s", instance.pk, name, blocked)
        return
    except Exception:
        # A failed react rolled back the consume+audit (#1879 atomicity) — the
        # approval survives for a retry; the FSM transition must never block.
        logger.exception("Failed to add Slack reactions for ticket %s transition %s", instance.pk, name)
        return
    if reacted:
        notify_user_on_behalf_post(
            target=target,
            action=f"transition_reaction:{name}",
            destination=f"ticket:{instance.pk} review message",
            artifact_url=instance.issue_url or target,
            summary=f"{name} transition reaction on ticket {instance.pk}",
        )


def _approval_reaction_target(pull_request: PullRequest) -> str:
    """Stable on-behalf-gate target identifier for the ``approve`` reaction.

    The recorded :class:`OnBehalfApproval` must scope to *this* specific PR
    so an approval for PR A never satisfies the reaction on PR B. The PR's
    ``url`` is the natural unique identifier and is what the user will
    type when recording the approval.
    """
    return pull_request.url or f"{pull_request.repo}#{pull_request.iid}"


def _add_approval_reaction_on_transition(
    instance: PullRequest,
    name: str,
    **_kwargs: object,
) -> None:
    """Post a ✅ on the requester's review message when a PR is approved (#961).

    The reaction is itself a post on the user's behalf, so it routes
    through the same recorded-approval gate every other on-behalf post
    uses (``require_on_behalf_approval`` — gate ON + recorded approval →
    proceed + audit; gate ON + no approval → skip without posting; gate
    OFF → proceed). Satisfiable without a TTY: the user records an
    :class:`OnBehalfApproval` scoped to (PR url, ``approval_reaction``)
    and the next approve transition publishes. The FSM transition itself
    is never blocked — only the on-behalf post is.
    """
    if name != "approve":
        return
    target = _approval_reaction_target(instance)
    try:
        reacted = require_on_behalf_approval(
            target=target,
            action="approval_reaction",
            publish=lambda: get_reaction_publisher().add_approval_reaction(instance),
        )
    except OnBehalfPostBlockedError as blocked:
        logger.info("Approval reaction for PR %s gated: %s", instance.pk, blocked)
        reacted = 0
    except Exception:
        # A failed react rolled back the consume+audit (#1879 atomicity); the
        # approval survives. Continue to the ReviewAssignment bookkeeping —
        # the FSM transition must never block on a reaction failure.
        logger.exception("Failed to add approval reaction for PR %s", instance.pk)
        reacted = 0
    if reacted:
        notify_user_on_behalf_post(
            target=target,
            action="approval_reaction",
            destination=f"PR {instance.url} review message",
            artifact_url=instance.url or target,
            summary=f"✅ approval reaction on PR {instance.url}",
        )
    # #1047: close the reaction-driven loop — mark every ReviewAssignment row
    # for this MR as ``approved`` so the audit trail captures the full
    # reaction → review → approve cycle. Best-effort: a missing row or DB
    # outage must never block the FSM transition.
    try:
        from teatree.core.models import ReviewAssignment  # noqa: PLC0415 — deferred: ORM import needs the app registry

        ReviewAssignment.approve_for_mr(mr_url=instance.url, overlay=instance.overlay)
    except Exception:
        logger.exception("Failed to mark ReviewAssignment approved for PR %s", instance.pk)


def _runs_in_session(instance: Task) -> bool:
    """True when the in-session ``/loop`` is the SOLE dispatcher for this task.

    Delegates to ``headless_dispatch.runs_in_session``: a ``(ticket.role, phase)``
    with a registered phase agent runs in-session ONLY under
    ``agent_runtime=interactive`` (the default), where it defaults to INTERACTIVE
    at creation (``Task.save`` chokepoint) and the ``execution_target`` guard below
    already skips it — this remains as defense-in-depth for a row that reaches
    HEADLESS some other way. Under a headless ``agent_runtime`` the same pair runs
    headless, so this returns ``False`` and the auto-enqueue ships it to
    ``execute_headless_task``. A pair with NO registered agent is free-form
    headless and is never in-session — never zero dispatch.
    """
    return runs_in_session(role=instance.ticket.role, phase=instance.phase)


def _auto_enqueue_headless_task(
    sender: type,  # noqa: ARG001 — Django signal receiver signature requires sender even when unused
    instance: Task,
    **_kwargs: object,
) -> None:
    """Auto-enqueue HEADLESS tasks for execution when created or re-routed.

    Under ``agent_runtime=interactive`` (default), loop-dispatched phase tasks
    default to INTERACTIVE at creation and so fail the ``execution_target`` guard;
    ``_runs_in_session`` stays as a belt-and-braces skip for any HEADLESS row of
    such a pair, so a ``db_worker`` draining the queue never double-runs loop phase
    work the in-session ``/loop`` owns. Under a headless ``agent_runtime`` the same
    phase tasks ARE headless and ``_runs_in_session`` is ``False``, so they are
    enqueued here like any other headless work.

    A task whose ticket names a non-empty unknown overlay is never enqueued
    (souliane/teatree#1959): dispatching it would crash ``execute_headless_task``
    — the drain safety-net fails such rows permanently instead. A blank overlay
    is the ambient single-overlay default and stays dispatchable.
    """
    if instance.execution_target != Task.ExecutionTarget.HEADLESS:
        return
    if instance.status != Task.Status.PENDING:
        return
    if _runs_in_session(instance):
        return
    if not instance.ticket.has_dispatchable_overlay():
        logger.warning("Skipping auto-enqueue of task %s: unknown overlay %r", instance.pk, instance.ticket.overlay)
        return
    from teatree.core.tasks import execute_headless_task  # noqa: PLC0415 — deferred: call-time import, kept lazy

    try:
        execute_headless_task.enqueue(int(instance.pk), instance.phase)
        logger.info("Auto-enqueued headless task %s (phase=%s)", instance.pk, instance.phase)
    except Exception:
        logger.exception("Failed to auto-enqueue headless task %s", instance.pk)


def _enqueue_ticket_transition_task(
    sender: type,  # noqa: ARG001 — Django signal receiver signature requires sender even when unused
    instance: Ticket,
    name: str,
    **_kwargs: object,
) -> None:
    """Enqueue the ``@task`` worker a ticket FSM transition body used to enqueue.

    The deferred import of the executor is call-time (mirroring
    ``_auto_enqueue_headless_task``), so a test patching ``tasks_mod.execute_*``
    still sees its stub. ``transaction.on_commit`` preserves the body's
    "state change + queued work land atomically" guarantee — the worker fires
    only after the transition's save commits.
    """
    executor_name = _TICKET_TRANSITION_TASKS.get(name)
    if executor_name is None:
        return
    from teatree.core import tasks as tasks_mod  # noqa: PLC0415 — deferred: call-time import, kept lazy

    executor = getattr(tasks_mod, executor_name)
    ticket_pk = int(instance.pk)
    transaction.on_commit(lambda: executor.enqueue(ticket_pk))


def _enqueue_worktree_transition_task(
    sender: type,  # noqa: ARG001 — Django signal receiver signature requires sender even when unused
    instance: Worktree,
    name: str,
    **_kwargs: object,
) -> None:
    """Enqueue the per-worktree ``@task`` worker a worktree FSM body used to enqueue.

    ``teardown`` is handled separately (``_enqueue_worktree_teardown_task``)
    because its body BLANKS ``db_name`` / ``extra`` before this receiver fires,
    so the worker needs the pre-blank snapshot, not the live (now-empty) row.
    """
    executor_name = _WORKTREE_TRANSITION_TASKS.get(name)
    if executor_name is None:
        return
    from teatree.core.worktree import worktree_tasks as worktree_tasks_mod  # noqa: PLC0415 — deferred: call-time import

    executor = getattr(worktree_tasks_mod, executor_name)
    worktree_pk = int(instance.pk)
    transaction.on_commit(lambda: executor.enqueue(worktree_pk))


def _enqueue_worktree_teardown_task(
    sender: type,  # noqa: ARG001 — Django signal receiver signature requires sender even when unused
    instance: Worktree,
    name: str,
    **_kwargs: object,
) -> None:
    """Enqueue ``execute_worktree_teardown`` with the PRE-BLANK snapshot (#2385 trap).

    ``Worktree.teardown()`` blanks ``db_name`` / ``extra`` on the row in its
    body, so reading them here would enqueue a teardown that never drops the
    database. The body stashes the pre-blank ``(db_name, extra)`` on the
    transient ``teardown_snapshot`` attribute; this receiver reads it and passes
    the non-blank values to the worker.
    """
    if name != "teardown":
        return
    from teatree.core.worktree import worktree_tasks as worktree_tasks_mod  # noqa: PLC0415 — deferred: call-time import

    worktree_pk = int(instance.pk)
    snapshot_db_name, snapshot_extra = instance.teardown_snapshot
    executor = worktree_tasks_mod.execute_worktree_teardown
    transaction.on_commit(lambda: executor.enqueue(worktree_pk, snapshot_db_name, snapshot_extra))


def _release_issue_markers_on_completion(
    sender: type,  # noqa: ARG001 — Django signal receiver signature requires sender even when unused
    instance: Ticket,
    target: str,
    **_kwargs: object,
) -> None:
    """Free the issue-implementer marker(s) when the ticket completes.

    Keyed on the ticket reaching a terminal state (MERGED / DELIVERED /
    IGNORED): a DISPATCHED/TICKET_CREATED marker held its budget slot for its
    whole life, so without this the first claim locked the single-ticket budget
    permanently. ABANDONED (give-up / fleet-claim-steal) is left untouched — it
    is already terminal and carries distinct semantics. Best-effort: the FSM
    transition must never block on the marker update.
    """
    if target not in _MARKER_RELEASE_TARGET_STATES or not instance.issue_url:
        return
    try:
        ImplementedIssueMarker.objects.filter(issue_url=instance.issue_url).exclude(
            state=ImplementedIssueMarker.State.ABANDONED
        ).update(state=ImplementedIssueMarker.State.COMPLETED)
    except Exception:
        logger.exception("Failed to release issue markers for ticket %s (%s)", instance.pk, instance.issue_url)


def register_signals() -> None:
    post_transition.connect(_log_ticket_transition, sender=Ticket, dispatch_uid="ticket_transition_audit")
    post_transition.connect(
        _enqueue_ticket_transition_task,
        sender=Ticket,
        dispatch_uid="ticket_transition_task_enqueue",
    )
    post_transition.connect(
        _enqueue_worktree_transition_task,
        sender=Worktree,
        dispatch_uid="worktree_transition_task_enqueue",
    )
    post_transition.connect(
        _enqueue_worktree_teardown_task,
        sender=Worktree,
        dispatch_uid="worktree_teardown_task_enqueue",
    )
    post_transition.connect(
        _add_slack_reactions_on_transition,
        sender=Ticket,
        dispatch_uid="ticket_transition_slack_reactions",
    )
    post_transition.connect(
        _add_approval_reaction_on_transition,
        sender=PullRequest,
        dispatch_uid="pull_request_approval_reaction",
    )
    post_transition.connect(
        _release_issue_markers_on_completion,
        sender=Ticket,
        dispatch_uid="ticket_completion_release_issue_markers",
    )
    post_save.connect(_auto_enqueue_headless_task, sender=Task, dispatch_uid="auto_enqueue_headless")
