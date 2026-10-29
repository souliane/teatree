"""Recording an authorizing review arms one merge critic at its head."""

from django.test import TestCase

from teatree.core.gates.merge_critic_arm import arm_for_recorded_review
from teatree.core.models import CriticDispatch, PullRequest, ReviewVerdict, Ticket


class TestRecordedReviewArming(TestCase):
    def test_pass_arms_once_and_hold_arms_none(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEW_REQUESTED)
        for pr_id in (5689, 5690):
            PullRequest.objects.create(
                ticket=ticket,
                overlay="t3-teatree",
                repo="souliane/teatree",
                iid=str(pr_id),
                url=f"https://github.com/souliane/teatree/pull/{pr_id}",
            )
        passed = ReviewVerdict.record(
            pr_id=5689,
            slug="souliane/teatree",
            reviewed_sha="a" * 40,
            verdict="merge_safe",
            reviewer_identity="reviewer-agent-7",
            ticket=ticket,
        )
        with self.captureOnCommitCallbacks(execute=True):
            arm_for_recorded_review(passed)
            arm_for_recorded_review(passed)

        assert CriticDispatch.objects.filter(ticket=ticket, transition="merge", head_sha="a" * 40).count() == 1
        claim = CriticDispatch.objects.get(ticket=ticket, transition="merge")
        assert claim.task is not None
        assert claim.task.phase == "critic_reviewing"

        held = ReviewVerdict.record(
            pr_id=5690,
            slug="souliane/teatree",
            reviewed_sha="b" * 40,
            verdict="hold",
            reviewer_identity="reviewer-agent-7",
            ticket=ticket,
        )
        with self.captureOnCommitCallbacks(execute=True):
            arm_for_recorded_review(held)
        assert not CriticDispatch.objects.filter(ticket=ticket, transition="merge", head_sha="b" * 40).exists()
