"""Persist the reviewing agent's retrieved source context before phase completion."""

from teatree.agents.result_schema import AgentResultBlob
from teatree.core.gates.anti_vacuity_gate import is_complete as anti_vacuity_complete
from teatree.core.gates.review_context_gate import is_complete
from teatree.core.modelkit.phases import normalize_phase
from teatree.core.models import Task
from teatree.core.models.ticket_worktree_checks import dispatch_worktree_path
from teatree.core.models.types import AntiVacuityAttestation, ReviewContext
from teatree.utils import git
from teatree.utils.run import CommandFailedError


def _record_author_anti_vacuity(task: Task, result: AgentResultBlob) -> str:
    """Bind the maker's self-review proof to its worktree head."""
    if task.ticket.role != task.ticket.Role.AUTHOR or not task.ticket.has_shippable_diff():
        return ""
    raw = result.get("anti_vacuity")
    if not isinstance(raw, dict):
        return "anti-vacuity recording refused: return the reviewing phase's anti_vacuity evidence"
    attestation = AntiVacuityAttestation(**raw)
    if not anti_vacuity_complete(attestation):
        return "anti-vacuity recording refused: AC coverage and proven tests or no_new_tests are required"
    workdir = dispatch_worktree_path(task.ticket)
    if not workdir:
        return "anti-vacuity recording refused: ticket has no dispatch worktree to bind to a head SHA"
    try:
        head_sha = git.head_sha(repo=workdir)
    except CommandFailedError as exc:
        return f"anti-vacuity recording refused: cannot read worktree head SHA: {exc}"
    task.ticket.record_anti_vacuity_attestation(
        head_sha,
        str(attestation["ac_coverage"]),
        [str(node) for node in attestation.get("proven_tests", [])],
        no_new_tests=attestation.get("no_new_tests") is True,
    )
    return ""


def record_returned_review_context(task: Task, result: AgentResultBlob, *, phase: str) -> str:
    if normalize_phase(phase or task.phase) != "reviewing":
        return ""
    if result.get("needs_user_input") is True:
        # A blocked reviewer has no verdict to attest. Task completion parks for
        # input instead of advancing the reviewing transition.
        return ""
    raw = result.get("review_context")
    if not isinstance(raw, dict):
        return (
            "review context recording refused: return review_context with the fetched work item, "
            "documents, and analysis"
        )
    context = ReviewContext(
        work_item=str(raw.get("work_item", "")),
        documents=raw.get("documents", []),
        analysis=str(raw.get("analysis", "")),
    )
    if not is_complete(context):
        return "review context recording refused: work_item, a downloaded document, and analysis are required"
    attestation_error = _record_author_anti_vacuity(task, result)
    if attestation_error:
        return attestation_error
    task.ticket.record_review_context(context["work_item"], context["documents"], context["analysis"])
    return ""
