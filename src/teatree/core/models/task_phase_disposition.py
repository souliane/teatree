"""FSM phase-transition disposition + wedge escalation for completed phase tasks.

Split out of ``task.py`` (the Task-model lifecycle module): these helpers decide
a completed phase task's FSM disposition — derive a transition's declared source
states, auto-ignore an unshippable REVIEWED ticket, and escalate a genuine FSM
wedge as a durable ``DeferredQuestion``. None of it is core Task lifecycle
(claim / lease / complete / route), so it lives in its own concern module.
"""

import logging
from typing import TYPE_CHECKING

from teatree.core.models.ticket import Ticket

if TYPE_CHECKING:
    from teatree.core.models.task import Task

logger = logging.getLogger(__name__)

#: The lifecycle-FSM target state each phase's completion should reach. A
#: completed phase task whose ticket sits BEHIND its target with no matching
#: guard is a genuine wedge (escalate); at-or-past is an idempotent replay
#: (no-op). A phase absent here is free-form work with no FSM transition.
_PHASE_TARGET_STATE: dict[str, str] = {
    "scoping": Ticket.State.STARTED,
    "planning": Ticket.State.PLANNED,
    "coding": Ticket.State.CODED,
    "testing": Ticket.State.TESTED,
    "reviewing": Ticket.State.REVIEWED,
    "shipping": Ticket.State.SHIPPED,
}
#: Lifecycle order used to compare a ticket's position to a phase's target.
#: Terminal/abandoned IGNORED is intentionally absent — it is never a wedge.
_STATE_ORDER: list[str] = [
    Ticket.State.NOT_STARTED,
    Ticket.State.SCOPED,
    Ticket.State.STARTED,
    Ticket.State.PLANNED,
    Ticket.State.CODED,
    Ticket.State.TESTED,
    Ticket.State.REVIEWED,
    Ticket.State.SHIPPED,
    Ticket.State.IN_REVIEW,
    Ticket.State.MERGED,
    Ticket.State.RETROSPECTED,
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
    """Auto-ignore a REVIEWED ticket ``review()`` found had no shippable diff.

    ``review()`` lands REVIEWED and stamps ``extra["shipping_skipped"]`` when
    there is no shippable diff (meta / already-shipped work). Without a
    disposition that ticket rests at REVIEWED forever — nothing consumes the
    marker, it never reaches a terminal state, and it holds its
    issue-implementer budget marker and its in-flight WIP slot indefinitely.
    Ignoring it is the explicit disposition: IGNORED is terminal (releasing
    the marker via the completion signal and freeing the WIP slot), and the
    ``shipping_skipped`` reason stays recorded in ``extra`` alongside
    ``ignored_from``.
    """
    if ticket.state != Ticket.State.REVIEWED:
        return
    extra = ticket.extra if isinstance(ticket.extra, dict) else {}
    if not extra.get("shipping_skipped"):
        return
    logger.info("Ticket %s reviewed with no shippable diff; auto-ignoring (terminal disposition)", ticket.pk)
    ticket.ignore()
    ticket.save()


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
    target = _PHASE_TARGET_STATE.get(phase)
    # IGNORED is the one state absent from _STATE_ORDER (terminal/abandoned,
    # never a wedge); excluding it here means ticket.state is always in the
    # order below, so the index lookups cannot raise.
    if target is None or ticket.state == Ticket.State.IGNORED:
        return
    if _STATE_ORDER.index(ticket.state) >= _STATE_ORDER.index(target):
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
    session_id: int | None = task.session_id  # ty: ignore[unresolved-attribute]
    DeferredQuestion.record(question, session_id=str(session_id or ""), tool_use_id=dedup_key)
