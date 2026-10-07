"""FSM phase-transition disposition + wedge escalation for completed phase tasks.

Split out of ``task.py`` (the Task-model lifecycle module): these helpers decide
a completed phase task's FSM disposition — derive a transition's declared source
states, auto-ignore an unshippable SELF_REVIEWED ticket, and escalate a genuine FSM
wedge as a durable ``DeferredQuestion``. None of it is core Task lifecycle
(claim / lease / complete / route), so it lives in its own concern module.
"""

import logging
from typing import TYPE_CHECKING

from teatree.core.modelkit.phases import normalize_phase, phase_spellings
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.plan_decision import has_plan_decision
from teatree.core.models.self_review import SelfReview
from teatree.core.models.ticket import Ticket
from teatree.core.repair_loop import max_phase_iterations

if TYPE_CHECKING:
    from collections.abc import Callable

    from teatree.core.models.task import Task

logger = logging.getLogger(__name__)

_CAP_QUESTION_FINDINGS_BUDGET = 8_000

#: The lifecycle-FSM target state each phase's completion should reach. A
#: completed phase task whose ticket sits BEHIND its target with no matching
#: guard is a genuine wedge (escalate); at-or-past is an idempotent replay
#: (no-op). A phase absent here is free-form work with no FSM transition.
_PHASE_TARGET_STATE: dict[str, str] = {
    "scoping": Ticket.State.WORK_STARTED,
    "planning": Ticket.State.PLAN_RECORDED,
    "coding": Ticket.State.CODED,
    "testing": Ticket.State.TESTED,
    "reviewing": Ticket.State.SELF_REVIEWED,
    "shipping": Ticket.State.PR_OPENED,
}
#: Lifecycle order used to compare a ticket's position to a phase's target.
#: The off-ladder terminals IGNORED and REVIEW_DELIVERED are intentionally absent —
#: a settled ticket is never a wedge (guarded via ``is_settled`` before the lookup).
_STATE_ORDER: list[str] = [
    Ticket.State.NOT_STARTED,
    Ticket.State.SCOPED,
    Ticket.State.WORK_STARTED,
    Ticket.State.PLAN_RECORDED,
    Ticket.State.CODED,
    Ticket.State.TESTED,
    Ticket.State.SELF_REVIEWED,
    Ticket.State.PR_OPENED,
    Ticket.State.REVIEW_REQUESTED,
    Ticket.State.MERGED,
    Ticket.State.RETRO_RECORDED,
    Ticket.State.DELIVERED,
]


def transition_source_states(name: str) -> set[str]:
    """The declared source states of the Ticket FSM transition *name* (derived, not hand-listed).

    Reads the ``@transition(source=[…])`` declaration straight off the FSM field
    so a branch guard can never drift from the transition it mirrors (the #808
    hand-duplication class). A ``source="*"`` wildcard is excluded — it carries
    no specific source to mirror.
    """
    fsm_field = Ticket._meta.get_field("state")  # noqa: SLF001 — Django's documented Model._meta API
    transitions = fsm_field.get_all_transitions(Ticket)  # ty: ignore[unresolved-attribute]  # django-fsm dynamic method
    return {str(t.source) for t in transitions if t.source != "*" and t.name == name}


def dispose_unshippable_review(ticket: Ticket) -> None:
    """Auto-ignore a SELF_REVIEWED ticket ``review()`` found had no shippable diff.

    ``review()`` lands SELF_REVIEWED and stamps ``extra["shipping_skipped"]`` when
    there is no shippable diff (meta / already-shipped work). Without a
    disposition that ticket rests at SELF_REVIEWED forever — nothing consumes the
    marker, it never reaches a terminal state, and it holds its
    issue-implementer budget marker and its in-flight WIP slot indefinitely.
    Ignoring it is the explicit disposition: IGNORED is terminal (releasing
    the marker via the completion signal and freeing the WIP slot), and the
    ``shipping_skipped`` reason stays recorded in ``extra`` alongside
    ``ignored_from``.
    """
    if ticket.state != Ticket.State.SELF_REVIEWED:
        return
    extra = ticket.extra if isinstance(ticket.extra, dict) else {}
    if not extra.get("shipping_skipped"):
        return
    logger.info("Ticket %s reviewed with no shippable diff; auto-ignoring (terminal disposition)", ticket.pk)
    ticket.ignore()
    ticket.save()


def advance_coded_ticket(task: "Task", ticket: Ticket) -> bool:
    """Fire a completed coding task's transition, or escalate a wedge; ``True`` iff one fired.

    Only the rework parented on a held self-review discharges it: any other coding run on a
    held ticket queues that rework instead, so the HOLD's own findings always reach a coder.
    """
    if ticket.state == Ticket.State.PLAN_RECORDED:
        ticket.code(parent_task=task)
    elif ticket.state in {Ticket.State.NOT_STARTED, Ticket.State.SCOPED, Ticket.State.WORK_STARTED} and (
        has_plan_decision(ticket)
    ):
        # A plan recorded off the WORK_STARTED rung (``ticket plan`` / ``skip-planning`` on an
        # early ticket) legitimately mints coding before PLAN_RECORDED; ``code_direct`` advances it.
        ticket.code_direct(parent_task=task)
    elif ticket.state in transition_source_states("address_self_review") and (held := SelfReview.open_hold_for(ticket)):
        if task.parent_task_id != held.task_pk:  # ty: ignore[unresolved-attribute]
            queue_self_review_rework(ticket, held)
            return False
        ticket.address_self_review(parent_task=task)
    else:
        escalate_unmatched_phase_transition(task, phase="coding", ticket=ticket)
        return False
    ticket.save()
    return True


def advance_self_reviewed_ticket(task: "Task", ticket: Ticket) -> bool:
    """Advance a TESTED ticket on its completed self-review, unless that review held."""
    if ticket.state != Ticket.State.TESTED:
        escalate_unmatched_phase_transition(task, phase="reviewing", ticket=ticket)
        return False
    if (review := SelfReview.of_task(task)) is not None and review.is_hold:
        queue_self_review_rework(ticket, review)
        return False
    ticket.review(parent_task=task)
    ticket.save()
    dispose_unshippable_review(ticket)
    return True


def advance_shipped_ticket(task: "Task", ticket: Ticket) -> bool:
    """Ship a SELF_REVIEWED ticket on its completed shipping task, through the same gates as ``pr create``.

    #1284 (codex #1282-2): the task-based completion path enforces the visited-phases gate
    ``_check_shipping_gate`` runs, so a SELF_REVIEWED ticket with missing testing/reviewing
    attestations cannot reach PR_OPENED through the task path. ``check_gate_across_ticket``
    raises ``QualityGateError`` when phases are missing, which propagates to the caller. A
    self-review HOLD the ticket was parked past stops it too, and queues that HOLD's rework.
    """
    if ticket.state != Ticket.State.SELF_REVIEWED:
        escalate_unmatched_phase_transition(task, phase="shipping", ticket=ticket)
        return False
    if (held := SelfReview.open_hold_for(ticket)) is not None:
        queue_self_review_rework(ticket, held)
        return False
    task.session.check_gate_across_ticket("shipping")
    ticket.ship()
    ticket.save()
    return True


def queue_self_review_rework(ticket: Ticket, review: SelfReview) -> None:
    """Queue *review*'s rework once, keyed on its parent link, or at the cap record why none is queued."""
    if ticket.tasks.filter(parent_task_id=review.task_pk, phase__in=phase_spellings("coding")).exists():
        return
    other_heads = SelfReview.held_heads(ticket) - {review.head_key}
    if len(other_heads) >= max_phase_iterations():
        _record_hold_cap(ticket, review, held_heads=len(other_heads) + 1)
        return
    ticket.schedule_self_review_rework(ticket.tasks.get(pk=review.task_pk), review)


def _record_hold_cap(ticket: Ticket, review: SelfReview, *, held_heads: int) -> None:
    """One INTERNAL row per ticket, sticky across answered and dismissed rows: the factory never pages on it."""
    marker = f"self-review-hold-cap:{ticket.pk}"
    if DeferredQuestion.objects.filter(dedupe_marker=marker).exists():
        return
    where = ticket.issue_url or f"ticket {ticket.pk}"
    findings = "\n".join(review.rendered_findings(budget=_CAP_QUESTION_FINDINGS_BUDGET))
    DeferredQuestion.record(
        f"[self-review-hold {where}] The self-review held {held_heads} heads of this ticket, so no rework is "
        f"queued for the HOLD at {review.reviewed_sha or 'an unrecorded head'} (reviewing task {review.task_pk}). "
        f"`t3 <overlay> ticket rework-hold {ticket.pk}` queues it by hand. Its findings:\n{findings}",
        dedupe_marker=marker,
        audience=DeferredQuestion.Audience.INTERNAL,
    )


AUTHOR_PHASE_ADVANCES: "dict[str, Callable[[Task, Ticket], bool]]" = {
    "coding": advance_coded_ticket,
    "reviewing": advance_self_reviewed_ticket,
    "shipping": advance_shipped_ticket,
}


def phase_output_reached(ticket: Ticket, phase: str) -> bool:
    """Whether *ticket* sits at or past the state *phase*'s completion targets.

    The full author ladder, REVIEW_REQUESTED through DELIVERED included — which is what
    ``Ticket.has_completed_phase`` deliberately does NOT answer (it stops at PR_OPENED, so
    a shipped ticket in peer review reads as though shipping never happened). An
    off-ladder state (REVIEW_DELIVERED / IGNORED) and a free-form phase both hold no
    position to compare, so both answer ``False``.
    """
    target = _PHASE_TARGET_STATE.get(normalize_phase(phase))
    if target is None or ticket.state not in _STATE_ORDER:
        return False
    if target == Ticket.State.CODED and ticket.owes_self_review_rework():
        return False
    return _STATE_ORDER.index(ticket.state) >= _STATE_ORDER.index(target)


def escalate_unmatched_phase_transition(task: "Task", *, phase: str, ticket: Ticket) -> None:
    """Escalate a genuine FSM wedge instead of the silent ``return False`` (#10).

    The FSM invariant: a lifecycle phase transition must never fail silently.
    When a completed phase task matches NO guard in
    :meth:`Task._apply_phase_transition`, the no-op is one of two things — an
    idempotent replay (the ticket has ALREADY advanced past this phase's
    target — a parallel child task, or a replay of an already-applied
    transition — expected, must NOT escalate), or a genuine wedge (the
    phase's work completed but the ticket is BEHIND the phase's target with
    no guard able to advance it — the class that left tickets 35/36 with
    completed coding yet zero transitions — must escalate, never drop).

    The two are told apart by comparing the ticket's state position to the
    phase's target state: at-or-past target is an idempotent replay; behind
    target is a wedge. A free-form (non-lifecycle) phase has no target and
    is expected to no-op. A terminal/abandoned ticket is never a wedge.
    """
    # A terminal ticket is never a wedge. REVIEW_DELIVERED/IGNORED are off the
    # author ladder, where :func:`phase_output_reached` answers False, so the
    # is_settled short-circuit is what keeps them out of the escalation.
    if _PHASE_TARGET_STATE.get(phase) is None or ticket.is_settled:
        return
    if phase_output_reached(ticket, phase):
        return  # idempotent replay — the ticket already advanced past this phase's target
    record_stuck_transition_question(task, phase=phase, ticket=ticket)


def record_stuck_transition_question(task: "Task", *, phase: str, ticket: Ticket) -> None:
    """Record a durable, deduped ``DeferredQuestion`` for an FSM wedge (§17.1 inv 9).

    Reuses the away-mode escalation queue (statusline / ``t3 teatree
    questions list`` / Slack DM drain) rather than a new surface — the same
    channel ``task_repair._escalate_stall`` uses. Deduped per (ticket,
    phase) on ``tool_use_id`` so an at-least-once replay of the same wedge
    does not flood the queue.
    """
    from teatree.core.models.deferred_question import DeferredQuestion  # noqa: PLC0415 — ORM/app-registry

    dedup_key = f"fsm-wedge:{ticket.pk}:{phase}"
    already = DeferredQuestion.objects.filter(
        tool_use_id=dedup_key,
        answered_at__isnull=True,
        dismissed_at__isnull=True,
    ).exists()
    if already:
        return
    where = ticket.issue_url or f"ticket {ticket.pk}"
    question = (
        f"FSM wedge on {where}: the {phase!r} phase completed (task {task.pk}) but no "
        f"lifecycle transition matched from state {ticket.state!r}, so the ticket cannot "
        f"advance and is stuck before {phase!r}. How should it proceed — rework the "
        f"earlier phases, or ignore?"
    )
    DeferredQuestion.record(question, task_session=task.session, tool_use_id=dedup_key)
