"""Bounded corrective instructions for failed phase attempts."""

from teatree.agents.envelope_refusal import (
    corrective_instruction,
    is_no_envelope_refusal,
    is_owner_stop_refusal,
    is_recorder_refusal,
    is_review_shape_refusal,
    is_rubric_grades_refusal,
    refused_envelope_instruction,
)
from teatree.agents.result_schema_owner_stop import owner_stop_limits_lines
from teatree.core.modelkit.phases import normalize_phase
from teatree.core.modelkit.task_failure_taxonomy import WRONG_HEAD_RETRY_PREFIX
from teatree.core.models import Task
from teatree.core.models.auto_review_dispatch import build_review_contract
from teatree.core.models.merge_clear import is_commit_sha

CORRECTIVE_PHASES = frozenset({"coding", "debugging"})
CORRECTIVE_MARKER = "[auto-corrective-retry]"


def corrective_note(task: Task, error: str) -> str | None:
    """The emit-the-envelope instruction *task* earns, or ``None`` if it must escalate.

    The RUNNER-side ``no_result_envelope`` refusal is a pure output-FORMAT failure, so
    it is corrective on ANY phase where ``ProseSummaryPolicy.allowed`` is False:
    ``debugging`` (already in :data:`CORRECTIVE_PHASES`) plus every no-evidence,
    non-prose-exempt phase outside it (``architectural_review``, ``bughunt``, ``e2e``,
    ``codex_*``) — which the old gate left paging a human on the first prose-only run.
    A RECORDER-side refusal stays gated on the set, except that a ``reviewing`` run whose
    verdict envelope was refused for a misnamed, misplaced or malformed key is told that
    refusal once — the reopened row resumes its own conversation, so the reviewer fixes
    the keys without repeating the review. A withheld verdict is still a judgement to
    surface. ``None`` for a genuine defect, and for a task whose one corrective retry is
    already spent.
    """
    if CORRECTIVE_MARKER in task.execution_reason:
        return None
    phase = normalize_phase(task.phase)
    if phase == "reviewing" and (head := wrong_head_retry_head(error)):
        return (
            f"Retry the review once at the live pull request head {head}. "
            f"Run `t3 review checkout {task.ticket.issue_url} --sha {head}` and verify HEAD. "
            "If checkout still resolves elsewhere, return review_head_mismatch with the observed SHA."
        )
    if specific := _specific_refusal_note(phase, error):
        return specific
    if is_no_envelope_refusal(error) or (phase in CORRECTIVE_PHASES and is_recorder_refusal(error)):
        return corrective_instruction(task.phase)
    if phase == "reviewing" and is_review_shape_refusal(error):
        return refused_envelope_instruction(error)
    return None


def _specific_refusal_note(phase: str, error: str) -> str | None:
    if is_owner_stop_refusal(error):
        limits = " ".join(owner_stop_limits_lines())
        return (
            f"Your owner decision card was refused ({error}). "
            f"Correct it once and return the complete result envelope. {limits}"
        )
    if phase == "reviewing" and is_rubric_grades_refusal(error):
        return (
            f"Your rubric grades were refused ({error}). Grade every criterion listed under TICKET RUBRIC "
            "as it reads now, then return the whole rubric_grades array and complete result envelope."
        )
    return None


def retry_review_contract(task: Task, head: str) -> str:
    dispatch = task.auto_review_dispatches.order_by("-pk").first()  # ty: ignore[unresolved-attribute]
    if dispatch is None:
        return task.execution_reason
    args = {"slug": dispatch.slug, "pr_id": dispatch.pr_id, "pr_url": dispatch.pr_url}
    previous = build_review_contract(**args, head_sha=dispatch.head_sha)
    current = build_review_contract(**args, head_sha=head)
    return task.execution_reason.replace(previous, current)


def wrong_head_retry_head(error: str) -> str:
    if not error.startswith(WRONG_HEAD_RETRY_PREFIX):
        return ""
    head = error.removeprefix(WRONG_HEAD_RETRY_PREFIX).partition(";")[0].strip().lower()
    return head if is_commit_sha(head) else ""
