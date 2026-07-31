"""The loop-side phase-task mint: one ``Session`` + initial ``Task`` per ticket+phase.

``persistence.py`` owns routing a dispatch signal to a ``Ticket``; this module owns
the decision to mint a ``Task`` for one, and the guard that stops two dispatchers
minting two. It is the sibling of ``Ticket._schedule_headless`` for the phases the
FSM scheduler does not cover, and holds the same idempotency contract keyed on the
same lock.
"""

import logging

from django.db import transaction

from teatree.core.models import Task, Ticket

logger = logging.getLogger(__name__)


def open_task_in_phase(ticket: Ticket, *, phase: str) -> Task | None:
    # #769 audit: match any accepted phase spelling (short verb or
    # gerund) via the shared SSOT helper, not a raw ``phase=phase``
    # filter that would miss a short-verb ``code`` task and let the
    # orchestrator create a duplicate.
    return Task.objects.pending_in_phase(phase).filter(ticket=ticket).first()


def has_open_task(ticket: Ticket, *, phase: str) -> bool:
    return open_task_in_phase(ticket, phase=phase) is not None


def create_phase_task(ticket: Ticket, *, phase: str, agent_id: str, reason: str) -> Task:
    """Create a fresh ``Session`` + initial ``Task`` for ``(ticket.role, phase)``.

    Mirrors ``ticket.schedule_coding`` / ``schedule_external_review`` for the
    phases those methods do not cover (``debugging``/``e2e``/``answering``/
    ``codex_reviewing``). ``Task.save`` routes a loop-dispatched ``(role, phase)``
    to INTERACTIVE under an ``interactive`` ``agent_runtime`` (the /loop slot is its
    dispatcher) and leaves it HEADLESS under the shipped headless one, so no explicit
    ``execution_target`` is set here.

    **Idempotent in its side effects, not merely in the state it converges to**
    (#3969) — the contract :meth:`Ticket._schedule_headless` holds at the FSM mint,
    keyed on the same lock. An in-flight sibling (a PENDING or CLAIMED Task on this
    ``(ticket, phase)``, in any accepted spelling) is RETURNED rather than raced.
    The zone handlers keep their :func:`has_open_task` pre-checks — those
    short-circuit before useless marker/claim work — but a pre-check is a
    read-then-write, so two dispatchers can both observe "no open task" before
    either writes; this guard is what makes them non-load-bearing for correctness.

    The check and the mint share one ``atomic`` block, so the guard is a real CAS:
    SQLite is opened in ``transaction_mode="IMMEDIATE"``, so the first writer holds
    the reserved lock for the whole block and a concurrent tick cannot pass the same
    check. Checking BEFORE the ``Session`` is created is what keeps a deduped call
    from leaving an orphan session row behind.

    A TERMINAL sibling is not a duplicate: a COMPLETED or FAILED task is a finished
    attempt, so the next call mints a fresh Session + Task. Deduping on "a task ever
    existed" would let one crashed attempt suppress the phase permanently.
    """
    from teatree.core.models.session import Session  # noqa: PLC0415 — lazy: avoids the models import cycle

    with transaction.atomic():
        in_flight = Task.objects.in_flight_for_phase(ticket.overlay, phase).filter(ticket=ticket).order_by("pk").first()
        if in_flight is not None:
            logger.info(
                "Reused in-flight %s task %s for ticket %s (status=%s) instead of minting a rival",
                phase,
                in_flight.pk,
                ticket.pk,
                in_flight.status,
            )
            return in_flight
        session = Session.objects.create(ticket=ticket, agent_id=agent_id)
        return Task.objects.create(ticket=ticket, session=session, phase=phase, execution_reason=reason)
