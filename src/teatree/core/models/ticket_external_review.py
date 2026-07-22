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

from teatree.core.models.errors import InvalidTransitionError
from teatree.core.models.session import Session
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket


def schedule_external_review(ticket: Ticket, *, parent_task: Task | None = None) -> Task:
    if ticket.role != Ticket.Role.REVIEWER:
        msg = f"schedule_external_review requires role=reviewer (got role={ticket.role!r})"
        raise InvalidTransitionError(msg)
    session = Session.objects.create(ticket=ticket, agent_id="external-review")
    return Task.objects.create(
        ticket=ticket,
        session=session,
        phase="reviewing",
        execution_target=Task.ExecutionTarget.HEADLESS,
        execution_reason=f"Auto-scheduled external review — review {ticket.issue_url}",
        parent_task=parent_task,
    )
