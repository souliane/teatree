"""Record every shell-denied hand-back that carries a maker≠checker write, short-circuiting on the first error."""

from teatree.agents.fix_record_recorder import record_returned_fix_record
from teatree.agents.result_schema import AgentResultBlob
from teatree.agents.review_context_recorder import record_returned_review_context
from teatree.agents.review_envelope_recorder import record_returned_review_envelope
from teatree.agents.review_request_recorder import record_returned_review_request
from teatree.agents.ship_readiness_recorder import record_returned_ship_readiness
from teatree.agents.sweep_proposals_recorder import record_returned_sweep_proposals
from teatree.agents.ticket_sweep_recorder import verify_returned_ticket_sweep
from teatree.core.gates.critic_gate import record_returned_critic_verdict
from teatree.core.gates.directive_interpret_gate import record_returned_directive_interpretation
from teatree.core.models import Task


def record_returned_envelopes(task: Task, result: AgentResultBlob, *, phase: str, executor_draft_run: bool) -> str:
    """Record each typed verdict/sketch a shell-denied headless phase RETURNS instead of writing.

    The orchestrator (a different actor) records it here. Each recorder is a no-op unless its own
    dispatch/verdict is present on the task, and returns an error string when the returned artifact is
    malformed or maker-graded — the first such error stops the chain so the caller fails the task.
    """
    context_error = record_returned_review_context(task, result, phase=phase)
    if context_error:
        return context_error
    review_error = record_returned_review_envelope(task, result, phase=phase)
    if review_error:
        return review_error
    critic_error = record_returned_critic_verdict(task, result)
    if critic_error:
        return critic_error
    fix_error = record_returned_fix_record(task, result)
    sweep_error = fix_error or verify_returned_ticket_sweep(result)
    draft_error = sweep_error or (
        executor_draft_run
        and (
            record_returned_review_request(task, result, phase=phase)
            or record_returned_ship_readiness(task, result, phase=phase)
            or record_returned_sweep_proposals(task, result, phase=phase)
        )
    )
    return draft_error or record_returned_directive_interpretation(task, result)
