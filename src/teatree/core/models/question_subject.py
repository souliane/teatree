"""The one definition of "the decision this question asks is already over".

It imports no model, so ``deferred_question`` can call it at record time without a cycle.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from teatree.core.models.deferred_question import DeferredQuestion
    from teatree.core.models.ticket import Ticket


def finished_subject_reason(question: "DeferredQuestion") -> str | None:
    ticket = _subject_ticket(question)
    if ticket is None:
        return None
    if ticket.state in type(ticket).finished_states():
        return f"the subject ticket is {ticket.state}"
    pull_requests = ticket.pull_requests.all()  # ty: ignore[unresolved-attribute]
    if pull_requests.exists() and not pull_requests.live().exists():
        return "every pull request of the subject is merged or closed"
    return None


def _subject_ticket(question: "DeferredQuestion") -> "Ticket | None":
    if question.parked_task is not None:
        return question.parked_task.ticket
    if question.task_session is not None:
        return question.task_session.ticket
    return None
