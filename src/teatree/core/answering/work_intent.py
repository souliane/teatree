"""Whether an answering task owes a work item, and the refusal when it does not deliver (#4527).

Every ``Ticket`` the reactive Slack-answer cycle dispatches came from a unit the
reader placed as work-implying — an instruction, a correction, or a question too
open to answer from recorded state. So the phase's answer alone is not the whole
deliverable: the request must also BECOME something intake can find. A run that
returns only the reply is indistinguishable from one that dropped the request,
which is why the omission is refused rather than logged.

The refusal reads as a recorder-side envelope refusal
(:mod:`teatree.agents.envelope_refusal`), so the existing one-shot corrective
re-dispatch reopens the task with the contract restated instead of parking it.
"""

from teatree.core.models import Task, Ticket

_SLACK_ANSWER_KEY = "slack_answer"


def owes_work_item(ticket: Ticket) -> bool:
    """Whether *ticket*'s dispatching message implied work the phase must place somewhere."""
    extra = ticket.extra if isinstance(ticket.extra, dict) else {}
    origin = extra.get(_SLACK_ANSWER_KEY)
    return isinstance(origin, dict) and origin.get("implies_work") is True


def missing_work_item_error(task: Task, result: dict) -> str:
    """The refusal for an answering run that owed a work item and returned none, else ``""``.

    Phrased with the shared "missing required evidence" marker so
    :func:`~teatree.agents.envelope_refusal.is_recorder_refusal` classifies it and the
    task earns the corrective retry rather than a human page.
    """
    if result.get("needs_user_input") or not owes_work_item(task.ticket):
        return ""
    if isinstance(result.get("work_item"), dict) and result["work_item"]:
        return ""
    return (
        "missing required evidence for phase 'answering': the dispatching message implied work, "
        "so the result must include a non-empty `work_item` naming what that work becomes "
        "(an `existing_issue_url` to attach to, a `title`+`body` to file, or a `no_work_reason`)"
    )


__all__ = ["missing_work_item_error", "owes_work_item"]
