"""Tests for ``PrApprovalScanner`` — revives the M7 merge_authorization lane (#8).

Anti-vacuity: the scanner emits an approval signal on a real forge-approval
payload (RED if the lane stays cold), and the twin — a not-yet-approved PR —
emits nothing. Outbound-gating floor: the revived ``approve`` transition's #961
Slack reaction does NOT fire at default settings (the on-behalf gate is ON) and
DOES fire when the gate is lifted.
"""

from unittest.mock import patch

from django.test import TestCase

import teatree.core.signals as signals_mod
from teatree.core.backend_protocols import ApprovalState
from teatree.core.models.pull_request import PullRequest
from teatree.core.models.ticket import Ticket
from teatree.loop.scanners.pr_approvals import PrApprovalScanner, sync_forge_approvals
from tests.teatree_core._on_behalf_gate_helpers import mode_immediate_cm


class _ApprovedHost:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def get_mr_approvals(self, *, repo: str, pr_iid: int) -> ApprovalState:
        self.calls.append((repo, pr_iid))
        return ApprovalState(approvals_left=0, approved_by=[], unresolved_resolvable=0)


class _UnapprovedHost:
    def get_mr_approvals(self, *, repo: str, pr_iid: int) -> ApprovalState:
        return ApprovalState(approvals_left=1, approved_by=[], unresolved_resolvable=0)


class _RaisingHost:
    def get_mr_approvals(self, *, repo: str, pr_iid: int) -> ApprovalState:
        msg = "forge is down"
        raise RuntimeError(msg)


class _FakeReactionPublisher:
    def __init__(self) -> None:
        self.approval_calls: list[object] = []

    def add_reactions_for_transition(self, ticket: object, transition_name: str) -> int:
        return 0

    def add_approval_reaction(self, pull_request: object) -> int:
        self.approval_calls.append(pull_request)
        return 1


class _PrApprovalScannerTestBase(TestCase):
    def _review_requested_pr(self, *, repo: str = "o/r", iid: str = "7") -> PullRequest:
        ticket = Ticket.objects.create(overlay="teatree", issue_url="https://x/70")
        pr = PullRequest.objects.create(
            ticket=ticket, overlay="teatree", url=f"https://github.com/{repo}/pull/{iid}", repo=repo, iid=iid
        )
        pr.request_review()
        pr.save()
        return pr


class TestPrApprovalScannerEmitsSignal(_PrApprovalScannerTestBase):
    def test_forge_approved_pr_emits_pr_approved_signal(self) -> None:
        pr = self._review_requested_pr()

        with mode_immediate_cm():
            signals = PrApprovalScanner(overlay="teatree", host=_ApprovedHost()).scan()

        assert [s.kind for s in signals] == ["pr.approved"]
        assert signals[0].payload["url"] == pr.url
        pr.refresh_from_db()
        assert pr.state == PullRequest.State.APPROVED

    def test_unapproved_pr_emits_nothing(self) -> None:
        pr = self._review_requested_pr()

        signals = PrApprovalScanner(overlay="teatree", host=_UnapprovedHost()).scan()

        assert signals == []
        pr.refresh_from_db()
        assert pr.state == PullRequest.State.REVIEW_REQUESTED

    def test_no_review_requested_rows_is_a_noop(self) -> None:
        assert PrApprovalScanner(overlay="teatree", host=_ApprovedHost()).scan() == []


class TestPrApprovalScannerOutboundGating(_PrApprovalScannerTestBase):
    """The revived lane must not fire the #961 approval reaction at default settings."""

    def test_no_reaction_when_gate_on_default(self) -> None:
        pr = self._review_requested_pr()
        publisher = _FakeReactionPublisher()

        # Gate ON (default draft_or_ask) — no recorded approval → the reaction
        # is skipped even though the PR does transition to APPROVED.
        with patch.object(signals_mod, "get_reaction_publisher", lambda: publisher):
            PrApprovalScanner(overlay="teatree", host=_ApprovedHost()).scan()

        pr.refresh_from_db()
        assert pr.state == PullRequest.State.APPROVED
        assert publisher.approval_calls == []

    def test_reaction_fires_when_gate_lifted(self) -> None:
        pr = self._review_requested_pr()
        publisher = _FakeReactionPublisher()

        with mode_immediate_cm(), patch.object(signals_mod, "get_reaction_publisher", lambda: publisher):
            PrApprovalScanner(overlay="teatree", host=_ApprovedHost()).scan()

        assert publisher.approval_calls == [pr]


class TestSyncForgeApprovals(_PrApprovalScannerTestBase):
    """#8: the pure per-row sync helper — the first production writer of APPROVED."""

    def test_forge_approved_pr_transitions_to_approved(self) -> None:
        pr = self._review_requested_pr()
        host = _ApprovedHost()

        with mode_immediate_cm():
            approved = sync_forge_approvals(host, [pr])

        assert [row.pk for row in approved] == [pr.pk]
        pr.refresh_from_db()
        assert pr.state == PullRequest.State.APPROVED
        assert host.calls == [("o/r", 7)]

    def test_unapproved_pr_stays_review_requested(self) -> None:
        pr = self._review_requested_pr()

        assert sync_forge_approvals(_UnapprovedHost(), [pr]) == []
        pr.refresh_from_db()
        assert pr.state == PullRequest.State.REVIEW_REQUESTED

    def test_non_review_requested_rows_are_skipped(self) -> None:
        ticket = Ticket.objects.create(overlay="teatree", issue_url="https://x/71")
        open_pr = PullRequest.objects.create(
            ticket=ticket, overlay="teatree", url="https://github.com/o/r/pull/12", repo="o/r", iid="12"
        )
        host = _ApprovedHost()

        assert sync_forge_approvals(host, [open_pr]) == []
        assert host.calls == []  # never polled — not in REVIEW_REQUESTED

    def test_forge_error_isolated_per_row(self) -> None:
        pr = self._review_requested_pr()

        assert sync_forge_approvals(_RaisingHost(), [pr]) == []
        pr.refresh_from_db()
        assert pr.state == PullRequest.State.REVIEW_REQUESTED
