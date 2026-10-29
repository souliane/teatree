"""Recording an authorizing review arms one merge critic at its head."""

from unittest.mock import MagicMock, patch

from django.test import TestCase

from teatree.core.backend_protocols import PrOpenState
from teatree.core.gates.merge_critic_arm import arm_for_recorded_review
from teatree.core.models import CriticDispatch, PullRequest, ReviewVerdict, Ticket


class TestRecordedReviewArming(TestCase):
    def test_pass_beside_standing_hold_does_not_arm(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEW_REQUESTED)
        PullRequest.objects.create(
            ticket=ticket,
            overlay="t3-teatree",
            repo="souliane/teatree",
            iid="5689",
            url="https://github.com/souliane/teatree/pull/5689",
        )
        ReviewVerdict.record(
            pr_id=5689,
            slug="souliane/teatree",
            reviewed_sha="a" * 40,
            verdict="hold",
            reviewer_identity="reviewer-agent-1",
            ticket=ticket,
        )
        passed = ReviewVerdict.record(
            pr_id=5689,
            slug="souliane/teatree",
            reviewed_sha="a" * 40,
            verdict="merge_safe",
            reviewer_identity="reviewer-agent-2",
            ticket=ticket,
        )
        with (
            patch("teatree.core.gates.merge_critic_arm._reviewed_pr_is_current", return_value=True),
            self.captureOnCommitCallbacks(execute=True),
        ):
            arm_for_recorded_review(passed)
        assert not CriticDispatch.objects.filter(ticket=ticket, transition="merge", head_sha="a" * 40).exists()

    def test_unverified_record_beside_pass_does_not_arm(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEW_REQUESTED)
        PullRequest.objects.create(
            ticket=ticket,
            overlay="t3-teatree",
            repo="souliane/teatree",
            iid="5689",
            url="https://github.com/souliane/teatree/pull/5689",
        )
        ReviewVerdict.record(
            pr_id=5689,
            slug="souliane/teatree",
            reviewed_sha="a" * 40,
            verdict="merge_safe",
            reviewer_identity="reviewer-agent-1",
            ticket=ticket,
        )
        unverified = ReviewVerdict.record(
            pr_id=5689,
            slug="souliane/teatree",
            reviewed_sha="a" * 40,
            verdict="unverified",
            reviewer_identity="reviewer-agent-2",
            evidence_gaps=["ci_unreadable"],
            ticket=ticket,
        )
        with (
            patch("teatree.core.gates.merge_critic_arm._reviewed_pr_is_current", return_value=True),
            self.captureOnCommitCallbacks(execute=True),
        ):
            arm_for_recorded_review(unverified)
        assert not CriticDispatch.objects.filter(ticket=ticket, transition="merge", head_sha="a" * 40).exists()

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
        with (
            patch("teatree.core.gates.merge_critic_arm._reviewed_pr_is_current", return_value=True),
            self.captureOnCommitCallbacks(execute=True),
        ):
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

    def test_closed_or_superseded_pr_does_not_arm_a_critic(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEW_REQUESTED)
        pr_url = "https://github.com/souliane/teatree/pull/5689"
        PullRequest.objects.create(ticket=ticket, overlay="t3-teatree", repo="souliane/teatree", iid="5689", url=pr_url)
        passed = ReviewVerdict.record(
            pr_id=5689,
            slug="souliane/teatree",
            reviewed_sha="a" * 40,
            verdict="merge_safe",
            reviewer_identity="reviewer-agent-7",
            ticket=ticket,
        )
        host = MagicMock()
        provider = MagicMock()
        provider.get_code_host_for_url.return_value = host
        with (
            patch("teatree.core.gates.merge_critic_arm.get_backend_provider", return_value=provider),
            patch("teatree.core.gates.merge_critic_arm.get_overlay_for_ticket", return_value=object()),
        ):
            for state, live_head in ((PrOpenState.CLOSED, "a" * 40), (PrOpenState.OPEN, "b" * 40)):
                host.get_pr_open_state.return_value = state
                host.fetch_live_head_sha.return_value = live_head
                with self.captureOnCommitCallbacks(execute=True):
                    arm_for_recorded_review(passed)
                assert not CriticDispatch.objects.filter(ticket=ticket, transition="merge").exists()
            host.get_pr_open_state.return_value = PrOpenState.OPEN
            host.fetch_live_head_sha.return_value = "a" * 40
            with self.captureOnCommitCallbacks(execute=True):
                arm_for_recorded_review(passed)

        assert CriticDispatch.objects.filter(ticket=ticket, transition="merge", head_sha="a" * 40).exists()
