"""Schedule a reviewing task for a reviewer-role ticket (external PR).

Reviewer-role tickets represent PRs the user is requested to review in someone
else's repo — they have no implementation/test/ship phases. After the review
task completes, the ticket short-circuits to the reviewer terminal
``REVIEW_POSTED`` via ``mark_reviewed_externally`` (never ``DELIVERED``, which
means author work merged to main).

Lives in its own module (not on ``Ticket``) to keep the model's public-method
count and LOC under the project's module-health cap; semantically it is a
sibling of ``ticket.schedule_coding`` and friends. Unlike ``ticket.py`` this
module is never imported during Django model registration, so it can import the
sibling models at top level without the intra-package import cycle ``ticket.py``
must dodge with function-scoped imports.
"""

from django.db import transaction
from django_fsm import can_proceed

from teatree.core.models.errors import InvalidTransitionError
from teatree.core.models.session import Session
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket

REVIEW_PHASE = "reviewing"


def schedule_external_review(
    ticket: Ticket,
    *,
    parent_task: Task | None = None,
    pr_settled: bool = False,
) -> Task | None:
    """Mint (or reuse) the reviewing Task for a reviewer-role *ticket*, or retire it.

    Idempotent in its side effects, the same contract
    :func:`~teatree.loop.persistence_phase_task.create_phase_task` holds and keyed on
    the same lock: an in-flight sibling is RETURNED rather than raced. Callers used to
    lean on their own read-time "no active task" pre-check, which is a read-then-write —
    two dispatchers (the FSM scheduler and the stuck-ticket repair sweep) could both
    observe none and both mint one, giving the ticket two reviewers. Checking BEFORE the
    ``Session`` is created is what keeps a deduped call from orphaning a session row.

    ``pr_settled`` is True when the caller already confirmed — via a live forge read
    such as :func:`teatree.backends.loader.pr_is_merged_or_closed` — that the PR/MR
    this ticket reviews has already merged or closed. ``teatree.core.models`` may not
    import ``teatree.backends`` (the dependency runs the other way per the tach DAG),
    so the read is INJECTED rather than performed here; the DECISION is not, which is
    what keeps #4847's flood (~400 review tasks minted for dead PRs) from recurring
    through a caller that forgets to check. A settled PR mints nothing and retires the
    ticket instead (see :func:`_retire_settled_review`).
    """
    if ticket.role != Ticket.Role.REVIEWER:
        msg = f"schedule_external_review requires role=reviewer (got role={ticket.role!r})"
        raise InvalidTransitionError(msg)
    if pr_settled:
        _retire_settled_review(ticket)
        return None
    with transaction.atomic():
        in_flight = (
            Task.objects.in_flight_for_phase(ticket.overlay, REVIEW_PHASE).filter(ticket=ticket).order_by("pk").first()
        )
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


def _retire_settled_review(ticket: Ticket) -> None:
    """Land a reviewer ticket on its terminal when its PR settled with nothing to post.

    Mirrors ``board_reconcile._close_review``. Idempotent via ``can_proceed``: a
    ticket already at REVIEW_POSTED (or any state ``mark_review_no_action`` does not
    accept as a source) is a silent no-op rather than a raised ``TransitionNotAllowed``.
    """
    if not can_proceed(ticket.mark_review_no_action):
        return
    with transaction.atomic():
        ticket.mark_review_no_action()
        ticket.save()
