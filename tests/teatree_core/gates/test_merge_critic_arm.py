"""Recording an authorizing review arms one merge critic at its head."""

from unittest.mock import MagicMock, patch

from django.test import TestCase

from teatree.core.backend_protocols import PrOpenState
from teatree.core.gates.merge_critic_arm import arm_for_recorded_review
from teatree.core.models import CriticDispatch, MergeClear, PullRequest, ReviewVerdict, Ticket

_SLUG = "souliane/teatree"
_PR_ID = 5689
_HEAD = "a" * 40


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

    def test_lone_hold_does_not_arm(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEW_REQUESTED)
        PullRequest.objects.create(
            ticket=ticket,
            overlay="t3-teatree",
            repo="souliane/teatree",
            iid="5689",
            url="https://github.com/souliane/teatree/pull/5689",
        )
        held = ReviewVerdict.record(
            pr_id=5689,
            slug="souliane/teatree",
            reviewed_sha="a" * 40,
            verdict="hold",
            reviewer_identity="reviewer-agent-1",
            ticket=ticket,
        )
        with (
            patch("teatree.core.gates.merge_critic_arm._reviewed_pr_is_current", return_value=True),
            self.captureOnCommitCallbacks(execute=True),
        ):
            arm_for_recorded_review(held)
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

    def test_pass_arms_once(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEW_REQUESTED)
        PullRequest.objects.create(
            ticket=ticket,
            overlay="t3-teatree",
            repo="souliane/teatree",
            iid="5689",
            url="https://github.com/souliane/teatree/pull/5689",
        )
        passed = ReviewVerdict.record(
            pr_id=5689,
            slug="souliane/teatree",
            reviewed_sha="a" * 40,
            verdict="merge_safe",
            reviewer_identity="reviewer-agent-7",
            ticket=ticket,
        )
        host = MagicMock()
        host.get_pr_open_state.return_value = PrOpenState.OPEN
        host.fetch_live_head_sha.return_value = "a" * 40
        provider = MagicMock()
        provider.get_code_host_for_url.return_value = host
        with (
            patch("teatree.core.gates.merge_critic_arm.get_backend_provider", return_value=provider),
            patch("teatree.core.gates.merge_critic_arm.get_overlay_for_ticket", return_value=object()),
            self.captureOnCommitCallbacks(execute=True),
        ):
            arm_for_recorded_review(passed)
            arm_for_recorded_review(passed)

        assert CriticDispatch.objects.filter(ticket=ticket, transition="merge", head_sha="a" * 40).count() == 1
        claim = CriticDispatch.objects.get(ticket=ticket, transition="merge")
        assert claim.task is not None
        assert claim.task.phase == "critic_reviewing"

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

    def _recorded_pass(self, *, pr_state: str | None) -> tuple[Ticket, ReviewVerdict]:
        """A recorded merge_safe on a ticket whose PR row has *pr_state*, or only a CLEAR when ``None``."""
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEW_REQUESTED)
        if pr_state is None:
            MergeClear.objects.create(
                ticket=ticket,
                slug=_SLUG,
                pr_id=_PR_ID,
                reviewed_sha=_HEAD,
                reviewer_identity="reviewer-agent-7",
                gh_verify_result=MergeClear.VerifyResult.GREEN,
                blast_class=MergeClear.BlastClass.LOGIC,
            )
        else:
            PullRequest.objects.create(
                ticket=ticket,
                overlay="t3-teatree",
                repo=_SLUG,
                iid=str(_PR_ID),
                url=f"https://github.com/{_SLUG}/pull/{_PR_ID}",
                state=pr_state,
            )
        passed = ReviewVerdict.record(
            pr_id=_PR_ID,
            slug=_SLUG,
            reviewed_sha=_HEAD,
            verdict="merge_safe",
            reviewer_identity="reviewer-agent-7",
            ticket=ticket,
        )
        return ticket, passed

    def test_unreadable_forge_does_not_arm_and_warns(self) -> None:
        ticket, passed = self._recorded_pass(pr_state=PullRequest.State.OPEN)
        host = MagicMock()
        host.get_pr_open_state.return_value = PrOpenState.OPEN
        host.fetch_live_head_sha.side_effect = RuntimeError("forge down")
        provider = MagicMock()
        provider.get_code_host_for_url.return_value = host
        with (
            patch("teatree.core.gates.merge_critic_arm.get_backend_provider", return_value=provider),
            patch("teatree.core.gates.merge_critic_arm.get_overlay_for_ticket", return_value=object()),
            self.assertLogs("teatree.core.gates.merge_critic_arm", level="WARNING") as logs,
            self.captureOnCommitCallbacks(execute=True),
        ):
            arm_for_recorded_review(passed)

        assert f"Could not verify live PR head before arming critic for {_SLUG}#{_PR_ID}" in logs.output[0]
        assert not CriticDispatch.objects.filter(ticket=ticket, transition="merge").exists()

    def test_pr_without_a_row_or_already_settled_locally_never_reads_the_forge(self) -> None:
        for pr_state in (None, PullRequest.State.MERGED, PullRequest.State.CLOSED):
            ticket, passed = self._recorded_pass(pr_state=pr_state)
            provider = MagicMock()
            with (
                self.subTest(pr_state=pr_state),
                patch("teatree.core.gates.merge_critic_arm.get_backend_provider", return_value=provider),
                self.assertNoLogs(level="ERROR"),
                self.captureOnCommitCallbacks(execute=True),
            ):
                arm_for_recorded_review(passed)
            provider.get_code_host_for_url.assert_not_called()
            assert not CriticDispatch.objects.filter(ticket=ticket, transition="merge").exists()
            ticket.delete()
