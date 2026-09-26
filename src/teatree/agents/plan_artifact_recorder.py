"""Record a planning envelope's plan as the ticket's ``PlanArtifact`` (and its rubric rows).

The sibling of :mod:`teatree.agents.fix_record_recorder`: one envelope channel, one
guarded model factory. It returns the refusal rather than raising it, because a plan
``PlanArtifact.record`` rejects is the PLANNER's failure — the caller lands it as a
FAILED ``TaskAttempt`` the requeue sweep can read, where an escaping exception would
strand the task CLAIMED with no attempt and no diagnostic.
"""

from teatree.agents.result_schema import AgentResultBlob
from teatree.core.merge.ticket_resolution import resolve_gated_ticket
from teatree.core.modelkit.phases import normalize_phase
from teatree.core.models import Task, Ticket
from teatree.core.models.plan_artifact import PlanArtifact
from teatree.core.models.review_target import review_target_for_ticket


def record_returned_plan(task: Task, result: AgentResultBlob, *, phase: str) -> str:
    """Record *result*'s plan; the refusal text, or ``""`` when recorded or not applicable."""
    if normalize_phase(phase or task.phase) != "planning":
        return ""
    plan_text = result.get("plan_text")
    if not isinstance(plan_text, str) or not plan_text.strip():
        return ""
    target_ticket = _plan_target_ticket(task.ticket)
    if target_ticket is None:
        return (
            f"ticket {task.ticket.pk} is a reviewer-role ticket for a pull request whose gated ticket "
            "could not be resolved — refusing to record the plan's acceptance criteria on the reviewer "
            "ticket itself, where the merge-time rubric gate would never read them"
        )
    base_sha = result.get("base_sha")
    adequacy = result.get("adequacy")
    try:
        PlanArtifact.record(
            ticket=target_ticket,
            plan_text=plan_text,
            recorded_by=(task.session.agent_id or "").strip() or "planning",
            base_sha=base_sha if isinstance(base_sha, str) else "",
            adequacy=adequacy if isinstance(adequacy, dict) else None,
        )
    except ValueError as exc:  # RubricError is a ValueError — a refused checklist lands the same way
        return str(exc)
    return ""


def _plan_target_ticket(ticket: Ticket) -> Ticket | None:
    """*ticket*, unless it is a reviewer-role ticket keyed by a pull request — then the ticket its merge gates.

    A plan recorded on a REVIEWER ticket (its ``issue_url`` names a PR, not an issue) must
    land its rubric criteria on the GATED ticket the merge-time gate actually reads
    (:func:`resolve_gated_ticket` — the same resolver ``review_envelope_recorder`` uses),
    never on the reviewer ticket itself: a rubric there is a checklist no gate ever consults
    (souliane/teatree#4832). ``None`` when *ticket* IS a reviewer ticket but its gated ticket
    does not resolve — the caller refuses rather than silently recording nowhere-read criteria.
    An ordinary issue-keyed ticket (no PR target) is unaffected and returned unchanged.
    """
    target = review_target_for_ticket(ticket)
    if target is None:
        return ticket
    return resolve_gated_ticket(slug=target.slug, pr_id=target.pr_id)
