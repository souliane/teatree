"""The refusals a dispatch answers before its harness opens."""

from typing import TYPE_CHECKING

from teatree.agents.runner_budget import TicketBudget
from teatree.core.gates.closed_issue_dispatch_gate import closed_issue_dispatch_refusal
from teatree.core.gates.plan_dispatch_gate import unplanned_dispatch_refusal
from teatree.core.gates.review_recordability_gate import unrecordable_review_refusal

if TYPE_CHECKING:
    from teatree.core.models import Task


def pre_harness_refusal(task: "Task", *, phase: str) -> str | None:
    """Why this dispatch must not run at all, or ``None`` to proceed.

    Every check is resolved BEFORE the harness (souliane/teatree#2916): for a
    resumed pydantic_ai task, resolving the harness destructively pops the parked
    ancestor's thread, and a refused dispatch must never trigger that pop or the
    conversation is lost even though the run never starts.

    Order is cheapest-first: the plan, unrecordable-review and budget checks read the
    local DB, while the closed-issue check costs a forge round trip, so it runs only once
    the free refusals have passed.
    """
    plan_refusal = unplanned_dispatch_refusal(task.ticket, phase=phase)
    if plan_refusal is not None:
        return plan_refusal
    unrecordable = unrecordable_review_refusal(task, phase=phase)
    if unrecordable is not None:
        return unrecordable
    budget_refusal = TicketBudget.from_settings().breach_reason(task.ticket)
    if budget_refusal is not None:
        return budget_refusal
    return closed_issue_dispatch_refusal(task.ticket, phase=phase)
