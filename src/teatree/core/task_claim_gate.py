"""The lane brake checked between task enqueue and task claim."""

from typing import TYPE_CHECKING

from teatree.core.agent_admission import agent_admission_verdict

if TYPE_CHECKING:
    from teatree.core.models import Task


def pending_lane_denial(task_obj: "Task") -> str | None:
    if task_obj.status != task_obj.Status.PENDING:
        return None
    return agent_admission_verdict().claim_denied_reason(task_obj.phase, seated=task_obj.admitted_at is not None)
