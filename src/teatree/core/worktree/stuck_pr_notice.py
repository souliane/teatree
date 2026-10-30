"""Durable owner decision for a pull-request obligation that keeps failing to drain."""

import logging

from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.question_card import CardOption, QuestionCard
from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionError
from teatree.core.models.pending_pull_request import PendingPullRequest

MARKER_PREFIX = "pending-pr-stuck:"
_QUESTION = "Should I keep trying to open this pull request?"
logger = logging.getLogger(__name__)


def plain_failure_cause(error: str) -> str:
    lower = error.lower()
    if "not on remote" in lower or "not pushed" in lower:
        return "The branch has not reached its remote yet."
    if "fleet" in lower or "claim" in lower:
        return "Another run still holds the branch claim."
    if "conflict" in lower:
        return "The branch conflicts with its target."
    if "debt" in lower or "budget" in lower:
        return "A shipping gate is blocking the pull request."
    return "The last attempt could not open the pull request."


def ask_owner_about_stuck_pr(row: PendingPullRequest) -> DeferredQuestion | None:
    card = QuestionCard(
        decision=OwnerDecision.IRREVERSIBLE,
        checked=(
            f"The pull request is still owed after {row.drain_attempts} attempts.",
            "The pull-request obligation is still unresolved.",
        ),
        blocker="Only you can stop reminders while a pull request is still owed.",
        why=plain_failure_cause(row.last_error or row.reason),
        options=(
            CardOption("Keep trying", "I continue trying to open the pull request.", recommended=True),
            CardOption("Stop reminding", "I retire this reminder and leave the branch untouched."),
        ),
    )
    try:
        return DeferredQuestion.record(_QUESTION, dedupe_marker=f"{MARKER_PREFIX}{row.pk}", card=card)
    except DeferredQuestionError:
        logger.exception("stuck pull-request owner card refused")
        return None


def stop_reminding_if_answered(row: PendingPullRequest) -> bool:
    answer = DeferredQuestion.objects.filter(
        dedupe_marker=f"{MARKER_PREFIX}{row.pk}",
        answered_at__isnull=False,
        answer_text="Stop reminding",
        resolved_via=DeferredQuestion.ResolvedVia.SLACK,
    ).first()
    if answer is None or not answer.answered_on_owner_channel:
        return False
    PendingPullRequest.objects.discharge(repo_path=row.repo_path, branch=row.branch)
    return True


def settle_discharged_stuck_pr_notices() -> None:
    owed_markers = [f"{MARKER_PREFIX}{pk}" for pk in PendingPullRequest.objects.values_list("pk", flat=True)]
    discharged = DeferredQuestion.owner_pending().filter(dedupe_marker__startswith=MARKER_PREFIX)
    for question in discharged.exclude(dedupe_marker__in=owed_markers):
        question.mark_stale("the pull-request obligation was settled", resolver_id="pending_pr_settled")
