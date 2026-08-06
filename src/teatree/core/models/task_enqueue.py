"""The one create path for a phase task, shared by ``tasks create`` and the dashboard (#4085).

Prioritising a PR used to mean a raw ``db query`` to translate a PR number into a
ticket PK plus a ``tasks create`` call. The dashboard button needs the same create,
so it lives here rather than inline in the management command — one seam, so the CLI
and the button cannot drift apart in what a phase task is.

:func:`enqueue_phase_task` is the unconditional create the CLI keeps: the loop's phase
handoff enqueues freely, and a duplicate refusal there would break a legitimate retry.
:func:`enqueue_phase_task_once` is the dashboard's sibling — the same create behind an
idempotency guard, so a double-clicked button reports the queued task instead of adding
a second one.
"""

from typing import TYPE_CHECKING

from django.db import transaction

from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket

if TYPE_CHECKING:
    from django.db.models import QuerySet


class TaskEnqueueError(ValueError):
    """A refused enqueue — a blank phase or a blank reason."""


class DuplicatePhaseTaskError(TaskEnqueueError):
    """An unstarted task for this ticket and phase is already queued."""


def _unstarted_tasks(ticket: Ticket, phase: str) -> "QuerySet[Task]":
    """*ticket*'s unstarted tasks at *phase*, oldest first.

    ``PENDING`` only: a claimed task is being worked right now, and treating it as a
    blocker would leave an operator unable to re-queue a phase whose worker died.
    """
    return ticket.tasks.filter(phase=phase, status=Task.Status.PENDING).order_by("pk")  # ty: ignore[unresolved-attribute]


def enqueue_phase_task(
    *,
    ticket: Ticket,
    phase: str,
    reason: str,
    interactive: bool = False,
    agent_id: str = "phase-handoff",
) -> Task:
    """Create the phase task for *ticket*, on the ticket's canonical phase session.

    The returned row carries the PERSISTED execution target, which is not always the
    requested one — ``Task.save`` routes a loop-dispatched phase to the interactive
    lane whatever the caller asked for.
    """
    if not phase.strip():
        msg = "phase is required (scoping, coding, testing, reviewing, or shipping)."
        raise TaskEnqueueError(msg)
    if not reason.strip():
        msg = "a non-blank reason is required — it is the prompt body the worker receives."
        raise TaskEnqueueError(msg)
    session = ticket.resolve_phase_session(agent_id=agent_id)
    target = Task.ExecutionTarget.INTERACTIVE if interactive else Task.ExecutionTarget.HEADLESS
    return Task.objects.create(
        ticket=ticket,
        session=session,
        phase=phase,
        execution_target=target,
        execution_reason=reason,
    )


def enqueue_phase_task_once(
    *,
    ticket: Ticket,
    phase: str,
    reason: str,
    interactive: bool = False,
    agent_id: str = "dashboard",
) -> Task:
    """:func:`enqueue_phase_task`, refused while an unstarted task for the phase exists.

    Probe and create share one transaction with the candidate rows locked, so two
    clicks racing each other queue exactly one task rather than one each.
    """
    with transaction.atomic():
        existing = _unstarted_tasks(ticket, phase).select_for_update().first()
        if existing is not None:
            msg = f"TODO-{existing.pk} is already queued for {phase} — nothing to enqueue."
            raise DuplicatePhaseTaskError(msg)
        return enqueue_phase_task(
            ticket=ticket,
            phase=phase,
            reason=reason,
            interactive=interactive,
            agent_id=agent_id,
        )
