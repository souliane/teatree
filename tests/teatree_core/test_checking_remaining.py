from django.test import TestCase

from teatree.core._checking_gather import _merge_safe_prs
from teatree.core.checking_remaining import remaining_steps
from teatree.core.models import ReviewHoldDisposition, ReviewVerdict
from teatree.core.models.pull_request import PullRequest
from teatree.core.models.ticket import Ticket


def test_pre_pr_ladder_starts_after_current_state() -> None:
    assert remaining_steps(Ticket.State.CODED, "", has_pr=False, merge_safe=False)[:3] == (
        "testing",
        "factory review",
        "shipping",
    )


def test_pr_ladder_reopens_rework_and_respects_merge_safe() -> None:
    assert remaining_steps(Ticket.State.PR_OPENED, "coding", has_pr=True, merge_safe=True) == (
        "rework",
        "testing",
        "factory review",
        "independent review",
        "merge",
        "deploy",
    )
    assert remaining_steps(Ticket.State.REVIEW_REQUESTED, "", has_pr=True, merge_safe=True) == ("merge", "deploy")


def test_pr_with_a_testing_task_still_owes_factory_review() -> None:
    assert remaining_steps(Ticket.State.PR_OPENED, "testing", has_pr=True, merge_safe=False) == (
        "testing",
        "factory review",
        "independent review",
        "merge",
        "deploy",
    )


def test_review_variants_and_merged_work_keep_their_current_step() -> None:
    for phase in ("codex_reviewing", "codex_adversarial_reviewing", "e2e_reviewing", "critic_reviewing"):
        assert remaining_steps(Ticket.State.PR_OPENED, phase, has_pr=True, merge_safe=False)[:2] == (
            "factory review",
            "independent review",
        )
    assert remaining_steps(Ticket.State.MERGED, "coding", has_pr=False, merge_safe=False) == ("deploy",)
    assert remaining_steps(Ticket.State.RETRO_RECORDED, "coding", has_pr=False, merge_safe=False) == ("deploy",)


class TestCheckingReviewState(TestCase):
    def test_dispositioned_hold_leaves_merge_safe_pr_in_checking_report(self) -> None:
        slug = "souliane/teatree"
        sha = "feedfacecafebabe1234567890abcdef12345678"
        hold = ReviewVerdict.record(
            pr_id=4380, slug=slug, reviewed_sha=sha, verdict="hold", reviewer_identity="reviewer-a"
        )
        clearing = ReviewVerdict.record(
            pr_id=4380, slug=slug, reviewed_sha=sha, verdict="merge_safe", reviewer_identity="reviewer-b"
        )
        ReviewHoldDisposition.record(
            hold=hold,
            clearing_verdict=clearing,
            points=[{"index": -1, "classification": "disproved_premise", "citation": "fresh independent review"}],
        )

        assert _merge_safe_prs([PullRequest(iid="4380")]) == {(slug, 4380)}
