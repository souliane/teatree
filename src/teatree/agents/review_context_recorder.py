"""Persist the reviewing agent's retrieved source context before phase completion."""

from typing import cast

from teatree.agents.envelope_refusal import (
    ANTI_VACUITY_REFUSAL_PREFIX,
    REVIEW_CONTEXT_REFUSAL_PREFIX,
    returned_block_refusal,
)
from teatree.agents.result_schema import AgentResultBlob
from teatree.core.gates.review_context_gate import missing_review_context_fields
from teatree.core.modelkit.phases import normalize_phase
from teatree.core.models import Task
from teatree.core.models.ticket_evidence import anti_vacuity_problems
from teatree.core.models.ticket_worktree_checks import dispatch_worktree_path
from teatree.core.models.types import AntiVacuityAttestation, ReviewContext
from teatree.utils import git
from teatree.utils.run import CommandFailedError

#: A head the attestation cannot bind to is no envelope slip, so it stays outside the reviewer's corrective retry.
_UNBOUND_REFUSAL_PREFIX = "anti-vacuity head binding recording refused: "


def anti_vacuity_refusal(raw: object) -> str:
    """Why a returned ``anti_vacuity`` block proves nothing, naming each key — ``""`` when it does."""
    if not isinstance(raw, dict):
        return f"{ANTI_VACUITY_REFUSAL_PREFIX}missing anti_vacuity (ac_coverage, and proven_tests or no_new_tests)"
    problems = anti_vacuity_problems(AntiVacuityAttestation(**raw))
    return returned_block_refusal(ANTI_VACUITY_REFUSAL_PREFIX, "anti_vacuity", raw, problems) if problems else ""


def _record_author_anti_vacuity(task: Task, result: AgentResultBlob) -> str:
    """Bind the maker's self-review proof to its worktree head."""
    if task.ticket.role != task.ticket.Role.AUTHOR or not task.ticket.has_shippable_diff():
        return ""
    raw = result.get("anti_vacuity")
    if refusal := anti_vacuity_refusal(raw):
        return refusal
    attestation = cast("AntiVacuityAttestation", raw)
    workdir = dispatch_worktree_path(task.ticket)
    if not workdir:
        return f"{_UNBOUND_REFUSAL_PREFIX}ticket has no dispatch worktree to bind to a head SHA"
    try:
        head_sha = git.head_sha(repo=workdir)
    except CommandFailedError as exc:
        return f"{_UNBOUND_REFUSAL_PREFIX}cannot read worktree head SHA: {exc}"
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
        return f"{REVIEW_CONTEXT_REFUSAL_PREFIX}missing review_context (work_item, documents, analysis)"
    context = ReviewContext(
        work_item=str(raw.get("work_item", "")),
        documents=raw.get("documents", []),
        analysis=str(raw.get("analysis", "")),
    )
    if missing := missing_review_context_fields(context):
        return returned_block_refusal(
            REVIEW_CONTEXT_REFUSAL_PREFIX, "review_context", raw, [f"missing {', '.join(missing)}"]
        )
    attestation_error = _record_author_anti_vacuity(task, result)
    if attestation_error:
        return attestation_error
    task.ticket.record_review_context(context["work_item"], context["documents"], context["analysis"])
    return ""
