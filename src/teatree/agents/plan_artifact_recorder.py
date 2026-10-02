"""Record a planning envelope's plan as the ticket's ``PlanArtifact`` (and its rubric rows).

The sibling of :mod:`teatree.agents.fix_record_recorder`: one envelope channel, one
guarded model factory. It returns the refusal rather than raising it, because a plan
``PlanArtifact.record`` rejects is the PLANNER's failure — the caller lands it as a
FAILED ``TaskAttempt`` the requeue sweep can read, where an escaping exception would
strand the task CLAIMED with no attempt and no diagnostic.
"""

from teatree.agents.result_schema import AgentResultBlob
from teatree.core.modelkit.phases import normalize_phase
from teatree.core.models import Task
from teatree.core.models.plan_artifact import PlanArtifact


def record_returned_plan(task: Task, result: AgentResultBlob, *, phase: str) -> str:
    """Record *result*'s plan; the refusal text, or ``""`` when recorded or not applicable."""
    if normalize_phase(phase or task.phase) != "planning":
        return ""
    plan_text = result.get("plan_text")
    if not isinstance(plan_text, str) or not plan_text.strip():
        return ""
    base_sha = result.get("base_sha")
    adequacy = result.get("adequacy")
    try:
        PlanArtifact.record(
            ticket=task.ticket,
            plan_text=plan_text,
            recorded_by=(task.session.agent_id or "").strip() or "planning",
            base_sha=base_sha if isinstance(base_sha, str) else "",
            adequacy=adequacy if isinstance(adequacy, dict) else None,
        )
    except ValueError as exc:  # RubricError is a ValueError — a refused checklist lands the same way
        return str(exc)
    return ""
