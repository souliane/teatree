from unittest.mock import patch

from django.test import TestCase

import teatree.core.signals as signals_mod
from teatree.backends.slack import reactions
from teatree.core.models import OnBehalfApproval, OnBehalfAudit, PullRequest, ReviewAssignment, ReviewIntent, Ticket
from tests.teatree_core._on_behalf_gate_helpers import posture_forbids_cm
from tests.teatree_core.conftest import record_confirmed_merge_for_test


class _ReactionConfig:
    def get_transition_emojis(self) -> dict[str, str]:
        return {"mark_merged": "tada"}

    def get_slack_token(self) -> str:
        return "test-token"


class _ReactionOverlay:
    config = _ReactionConfig()


class TestReactionTargetBeforeApproval(TestCase):
    def test_transition_without_target_emits_no_blocked_log(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.REVIEW_REQUESTED)
        record_confirmed_merge_for_test(ticket)

        with posture_forbids_cm(), self.assertNoLogs(signals_mod.logger, level="INFO"):
            ticket.mark_merged()
            ticket.save()

        assert ticket.state == Ticket.State.MERGED
        assert not OnBehalfAudit.objects.filter(action="transition_reaction:mark_merged").exists()

    def test_transition_without_review_message_preserves_approval(self) -> None:
        for permalink in (None, "", "https://example.invalid/message"):
            with self.subTest(permalink=permalink):
                ticket = Ticket.objects.create(overlay="test", state=Ticket.State.REVIEW_REQUESTED)
                if permalink is not None:
                    ticket.extra = {"prs": {"one": {"review_permalink": permalink}}}
                    ticket.save(update_fields=["extra"])
                record_confirmed_merge_for_test(ticket)
                approval = OnBehalfApproval.record(
                    target=f"ticket:{ticket.pk}",
                    action="transition_reaction:mark_merged",
                    approver_id="operator",
                )

                with (
                    posture_forbids_cm(),
                    patch.object(reactions, "get_overlay", return_value=_ReactionOverlay()),
                    self.assertNoLogs(signals_mod.logger, level="INFO"),
                ):
                    ticket.mark_merged()
                    ticket.save()

                approval.refresh_from_db()
                assert ticket.state == Ticket.State.MERGED
                assert approval.consumed_at is None
                assert not OnBehalfAudit.objects.filter(approval=approval).exists()

    def test_transition_with_review_message_keeps_gated_publish(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.REVIEW_REQUESTED)
        ticket.extra = {"prs": {"one": {"review_permalink": "https://team.slack.com/archives/C9/p1700000000000100"}}}
        ticket.save(update_fields=["extra"])
        record_confirmed_merge_for_test(ticket)
        approval = OnBehalfApproval.record(
            target=f"ticket:{ticket.pk}",
            action="transition_reaction:mark_merged",
            approver_id="operator",
        )

        with (
            posture_forbids_cm(),
            patch.object(reactions, "get_overlay", return_value=_ReactionOverlay()),
            patch.object(reactions, "add_reaction_verified", return_value=True) as send,
        ):
            ticket.mark_merged()
            ticket.save()

        approval.refresh_from_db()
        assert approval.consumed_at is not None
        assert OnBehalfAudit.objects.filter(approval=approval).count() == 1
        send.assert_called_once_with("test-token", "C9", "1700000000.000100", "tada")

    def test_approval_without_review_message_preserves_approval_and_bookkeeping(self) -> None:
        for permalink in ("", "https://example.invalid/message"):
            with self.subTest(permalink=permalink):
                ticket = Ticket.objects.create(overlay="test")
                pr = PullRequest.objects.create(
                    ticket=ticket,
                    overlay="test",
                    url=f"https://gitlab.com/org/repo/-/merge_requests/{ticket.pk}",
                    repo="org/repo",
                    iid=str(ticket.pk),
                    state=PullRequest.State.OPEN,
                )
                pr.request_review(slack_url=permalink)
                pr.save()
                row = ReviewAssignment.record(
                    ReviewIntent(
                        mr_url=pr.url,
                        user_id="U0DEMOUSER1",
                        channel="C9",
                        slack_ts="1700000000.000100",
                        trigger="reaction",
                        overlay=pr.overlay,
                    )
                )
                assert row is not None
                approval = OnBehalfApproval.record(
                    target=pr.url,
                    action="approval_reaction",
                    approver_id="operator",
                )

                with (
                    posture_forbids_cm(),
                    self.assertNoLogs(signals_mod.logger, level="INFO"),
                    self.captureOnCommitCallbacks(execute=True),
                ):
                    pr.approve()
                    pr.save()

                approval.refresh_from_db()
                row.refresh_from_db()
                assert approval.consumed_at is None
                assert not OnBehalfAudit.objects.filter(approval=approval).exists()
                assert row.state == ReviewAssignment.State.APPROVED

    def test_approval_without_target_emits_no_blocked_log(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        pr = PullRequest.objects.create(
            ticket=ticket,
            overlay="test",
            url="https://gitlab.com/org/repo/-/merge_requests/8",
            repo="org/repo",
            iid="8",
            state=PullRequest.State.OPEN,
        )
        pr.request_review(slack_url="")
        pr.save()

        with (
            posture_forbids_cm(),
            self.assertNoLogs(signals_mod.logger, level="INFO"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            pr.approve()
            pr.save()

        assert pr.state == PullRequest.State.APPROVED
        assert not OnBehalfAudit.objects.filter(action="approval_reaction").exists()

    def test_approval_with_review_message_keeps_gated_publish(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        pr = PullRequest.objects.create(
            ticket=ticket,
            overlay="test",
            url="https://gitlab.com/org/repo/-/merge_requests/7",
            repo="org/repo",
            iid="7",
            state=PullRequest.State.OPEN,
        )
        pr.request_review(slack_url="https://team.slack.com/archives/C9/p1700000000000100")
        pr.save()
        approval = OnBehalfApproval.record(target=pr.url, action="approval_reaction", approver_id="operator")

        with (
            posture_forbids_cm(),
            patch.object(reactions, "get_overlay", return_value=_ReactionOverlay()),
            patch.object(reactions, "add_reaction_verified", return_value=True) as send,
            self.captureOnCommitCallbacks(execute=True),
        ):
            pr.approve()
            pr.save()

        approval.refresh_from_db()
        assert approval.consumed_at is not None
        assert OnBehalfAudit.objects.filter(approval=approval).count() == 1
        send.assert_called_once_with("test-token", "C9", "1700000000.000100", "white_check_mark")
