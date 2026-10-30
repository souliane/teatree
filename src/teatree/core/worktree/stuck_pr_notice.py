"""Durable owner decision for a pull-request obligation that keeps failing to drain."""

import logging
from collections.abc import Sequence

from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.question_card import CardOption, QuestionCard
from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionError
from teatree.core.models.pending_pull_request import PendingPullRequest

MARKER_PREFIX = "pending-pr-stuck:"
KEEP_TRYING = "Keep trying"
STOP_REMINDING = "Stop reminding"
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


def _marker(row: PendingPullRequest) -> str:
    return f"{MARKER_PREFIX}{row.pk}"


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
            CardOption(KEEP_TRYING, "I continue trying to open the pull request.", recommended=True),
            CardOption(STOP_REMINDING, "I retire this reminder and leave the branch untouched."),
        ),
        subject=f"pending-pr:{row.pk}",
    ).with_quote(_QUESTION, row.branch)
    try:
        return DeferredQuestion.record(_QUESTION, dedupe_marker=_marker(row), card=card)
    except DeferredQuestionError:
        logger.exception("stuck pull-request owner card refused")
        return None


def ask_owner_about_stuck_prs(rows: Sequence[PendingPullRequest]) -> None:
    """Ask once per overdue obligation, leaving an answered or pending card alone without a write."""
    cards = DeferredQuestion.objects.filter(dedupe_marker__startswith=MARKER_PREFIX)
    cards.answered_without_deciding((KEEP_TRYING, STOP_REMINDING)).release_markers()
    held = set(
        DeferredQuestion.objects.filter(dedupe_marker__in=[_marker(row) for row in rows]).values_list(
            "dedupe_marker", flat=True
        )
    )
    for row in rows:
        if _marker(row) not in held:
            ask_owner_about_stuck_pr(row)


def discharge_stopped(rows: Sequence[PendingPullRequest]) -> None:
    by_marker = {_marker(row): row for row in rows}
    stopped = (
        DeferredQuestion.answered_in_slack([STOP_REMINDING])
        .filter(dedupe_marker__in=list(by_marker))
        .values_list("dedupe_marker", flat=True)
    )
    for marker in stopped:
        PendingPullRequest.objects.discharge(repo_path=by_marker[marker].repo_path, branch=by_marker[marker].branch)


def settle_discharged_stuck_pr_notices() -> None:
    owed_markers = [f"{MARKER_PREFIX}{pk}" for pk in PendingPullRequest.objects.values_list("pk", flat=True)]
    discharged = DeferredQuestion.owner_pending().filter(dedupe_marker__startswith=MARKER_PREFIX)
    for question in discharged.exclude(dedupe_marker__in=owed_markers):
        question.mark_stale("the pull-request obligation was settled", resolver_id="pending_pr_settled")
