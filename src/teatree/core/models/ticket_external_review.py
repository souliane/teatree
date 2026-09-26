"""Schedule a reviewing task for a reviewer-role ticket (external PR).

Reviewer-role tickets represent PRs the user is requested to review in someone
else's repo — they have no implementation/test/ship phases. After the review
task completes, the ticket short-circuits to the reviewer terminal
``REVIEW_DELIVERED`` via ``mark_reviewed_externally`` (never ``DELIVERED``, which
means author work merged to main).

Lives in its own module (not on ``Ticket``) to keep the model's public-method
count and LOC under the project's module-health cap; semantically it is a
sibling of ``ticket.schedule_coding`` and friends. Unlike ``ticket.py`` this
module is never imported during Django model registration, so it can import the
sibling models at top level without the intra-package import cycle ``ticket.py``
must dodge with function-scoped imports.

Every reviewer mint first passes :func:`reviewer_dispatch_decline`: a review is owed
only while the ticket's state can still advance and the forge confirms the PR is OPEN.
"""

import logging
from enum import StrEnum

from django.db import transaction
from django_fsm import can_proceed

from teatree.core.modelkit.gate_registry import get_resolver
from teatree.core.models.errors import InvalidTransitionError
from teatree.core.models.session import Session
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket

logger = logging.getLogger(__name__)

REVIEW_PHASE = "reviewing"

# PrOpenState values: the model layer cannot import backend_protocols.
_PR_OPEN = "open"
_SETTLED_PR_STATES = frozenset({"merged", "closed"})


class ReviewDeclined(StrEnum):
    PR_STATE_UNKNOWN = "pr_state_unknown"
    NOTHING_OWED = "nothing_owed"
    RETIRED = "retired"


def schedule_external_review(ticket: Ticket, *, parent_task: Task | None = None) -> Task | ReviewDeclined:
    """Mint (or reuse) the reviewing Task for a reviewer-role *ticket*, or say why none is owed.

    Idempotent in its side effects, the same contract
    :func:`~teatree.loop.persistence_phase_task.create_phase_task` holds and keyed on
    the same lock: an in-flight sibling is RETURNED rather than raced. Callers used to
    lean on their own read-time "no active task" pre-check, which is a read-then-write —
    two dispatchers (the FSM scheduler and the stuck-ticket repair sweep) could both
    observe none and both mint one, giving the ticket two reviewers. Checking BEFORE the
    ``Session`` is created is what keeps a deduped call from orphaning a session row.
    """
    if ticket.role != Ticket.Role.REVIEWER:
        msg = f"schedule_external_review requires role=reviewer (got role={ticket.role!r})"
        raise InvalidTransitionError(msg)
    in_flight = _in_flight_review(ticket)
    if in_flight is not None:
        return in_flight
    decline = reviewer_dispatch_decline(ticket)
    if decline is not None:
        return decline
    with transaction.atomic():
        in_flight = _in_flight_review(ticket)
        if in_flight is not None:
            return in_flight
        session = Session.objects.create(ticket=ticket, agent_id="external-review")
        return Task.objects.create(
            ticket=ticket,
            session=session,
            phase=REVIEW_PHASE,
            execution_reason=f"Auto-scheduled external review — review {ticket.issue_url}",
            parent_task=parent_task,
        )


def reviewer_dispatch_decline(ticket: Ticket) -> ReviewDeclined | None:
    """Why no review task may be minted for reviewer *ticket* now, or ``None`` when one is owed.

    Cheapest first: a state a finished review cannot advance never reaches the forge, and
    the forge is read exactly once. A PR the forge cannot confirm OPEN fails closed.
    """
    if not can_proceed(ticket.mark_reviewed_externally, check_conditions=False):
        if ticket.state not in Ticket.State.values:
            logger.warning("Reviewer ticket %s is in unknown state %r — no review minted", ticket.pk, ticket.state)
        return ReviewDeclined.NOTHING_OWED
    pr_state = str(get_resolver("pr_open_state")(ticket))
    if pr_state in _SETTLED_PR_STATES:
        return ReviewDeclined.RETIRED if _retire(ticket, pr_state) else ReviewDeclined.NOTHING_OWED
    if pr_state != _PR_OPEN:
        return ReviewDeclined.PR_STATE_UNKNOWN
    return None


def _in_flight_review(ticket: Ticket) -> Task | None:
    return Task.objects.in_flight_for_phase(ticket.overlay, REVIEW_PHASE).filter(ticket=ticket).order_by("pk").first()


def _retire(ticket: Ticket, pr_state: str) -> bool:
    with transaction.atomic():
        if not can_proceed(ticket.ignore):
            logger.info(
                "Not retiring reviewer ticket %s: PR %s is %s but state %r has no ignore transition",
                ticket.pk,
                ticket.issue_url,
                pr_state,
                ticket.state,
            )
            return False
        ticket.ignore()
        ticket.save()
    logger.info("Retired reviewer ticket %s: PR %s is %s", ticket.pk, ticket.issue_url, pr_state)
    return True
