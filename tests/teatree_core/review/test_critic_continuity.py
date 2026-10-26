"""Critic contracts retain head-to-head evidence without treating missing reads as clean."""

from unittest.mock import patch

from django.test import TestCase

from teatree.core.models import CriticVerdict, Ticket
from teatree.core.review.critic_continuity import (
    merge_critic_continuity,
    merge_verdict_history,
    review_evidence_contract,
)
from teatree.quality.kill_matrix import KillMatrix, KillRow
from teatree.quality.review_evidence import ReviewEvidence
from teatree.utils.run import CommandFailedError


class TestCriticContinuity(TestCase):
    def test_critic_receives_head_bound_matrix_and_missing_receipt_is_report_only(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEW_REQUESTED)
        head = "a" * 40
        with patch("teatree.core.review.critic_continuity.dispatch_worktree_path", return_value=""):
            assert "REPORT ONLY" in review_evidence_contract(ticket, head)
            assert "return test_value unverified" not in review_evidence_contract(ticket, head)
        receipt = ReviewEvidence(head, (), KillMatrix(head, (KillRow("tests/test_x.py::test_a", ("m1",), (), (), ()),)))
        with (
            patch("teatree.core.review.critic_continuity.dispatch_worktree_path", return_value="/tmp/review"),
            patch("teatree.core.review.critic_continuity.read_review_evidence", return_value=receipt),
        ):
            assert "tests/test_x.py::test_a: killed=1 m1" in review_evidence_contract(ticket, head)
        inconclusive = ReviewEvidence(
            head, (), KillMatrix(head, (KillRow("tests/test_x.py::test_a", (), (), ("m1",), ()),))
        )
        with (
            patch("teatree.core.review.critic_continuity.dispatch_worktree_path", return_value="/tmp/review"),
            patch("teatree.core.review.critic_continuity.read_review_evidence", return_value=inconclusive),
        ):
            assert "REPORT ONLY" in review_evidence_contract(ticket, head)

    def test_delta_compares_the_two_pr_diffs_after_main_merges(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEW_REQUESTED)
        before, after = "b" * 40, "a" * 40
        CriticVerdict.record_from_envelope(
            ticket=ticket,
            transition="merge",
            head_sha=before,
            envelope={
                "grader_identity": "critic-agent-7",
                "items": [{"slug": "test_value", "status": "pass", "citation": "tests/x.py:1"}],
            },
        )

        def read_diff(*, repo: str, args: list[str]) -> str:
            if args == ["diff", "--unified=0", f"origin/main...{before}", "--"]:
                return "@@ -1 +1 @@\n-old\n+first\n"
            if args == ["diff", "--unified=0", f"origin/main...{after}", "--"]:
                return "@@ -1 +1 @@\n-old\n+second\n"
            return "main-only change\n" * 3000

        with (
            patch("teatree.core.review.critic_continuity.dispatch_worktree_path", return_value="/tmp/review"),
            patch("teatree.core.review.critic_continuity.git.run_strict", side_effect=read_diff) as read,
        ):
            contract = merge_critic_continuity(ticket, after)
        assert "second" in contract
        assert "first" in contract
        assert "main-only change" not in contract
        assert read.call_count == 2

    def test_delta_uses_the_ticket_target_branch(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEW_REQUESTED)
        before, after = "b" * 40, "a" * 40
        CriticVerdict.record_from_envelope(
            ticket=ticket,
            transition="merge",
            head_sha=before,
            envelope={
                "grader_identity": "critic-agent-7",
                "items": [{"slug": "test_value", "status": "pass", "citation": "tests/x.py:1"}],
            },
        )
        with (
            patch("teatree.core.review.critic_continuity.dispatch_worktree_path", return_value="/tmp/review"),
            patch(
                "teatree.core.review.critic_continuity.resolve_target_branch", return_value="origin/release"
            ) as target,
            patch("teatree.core.review.critic_continuity.git.run_strict", side_effect=["old", "new"]) as diff,
        ):
            contract = merge_critic_continuity(ticket, after)
        target.assert_called_once_with(ticket, "/tmp/review")
        assert "new" in contract
        assert diff.call_args_list[0].kwargs["args"][2] == f"origin/release...{before}"
        assert diff.call_args_list[1].kwargs["args"][2] == f"origin/release...{after}"

    def test_first_pass_demands_complete_review(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEW_REQUESTED)
        assert "FIRST PASS: inspect the complete PR diff" in merge_critic_continuity(ticket, "a" * 40)

    def test_unreadable_delta_is_explicitly_unverified(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEW_REQUESTED)
        CriticVerdict.record_from_envelope(
            ticket=ticket,
            transition="merge",
            head_sha="b" * 40,
            envelope={
                "grader_identity": "critic-agent-7",
                "items": [{"slug": "test_value", "status": "pass", "citation": "tests/test_x.py:5"}],
            },
        )
        with patch("teatree.core.review.critic_continuity.dispatch_worktree_path", return_value=""):
            assert "UNAVAILABLE" in merge_critic_continuity(ticket, "a" * 40)
        with (
            patch("teatree.core.review.critic_continuity.dispatch_worktree_path", return_value="/tmp/review"),
            patch(
                "teatree.core.review.critic_continuity.git.run_strict",
                side_effect=CommandFailedError(["git", "diff"], 1, "", "missing diff"),
            ),
        ):
            assert "UNAVAILABLE" in merge_critic_continuity(ticket, "a" * 40)


def _merge_verdict(ticket: Ticket, *, head: str, citation: str, transition: str = "merge") -> None:
    CriticVerdict.objects.create(
        ticket=ticket,
        transition=transition,
        head_sha=head,
        grader_identity="critic-agent-7",
        items=[{"slug": "test_value", "status": "fail", "citation": citation}],
    )


class TestMergeVerdictHistory(TestCase):
    def test_a_ticket_with_no_merge_verdict_has_no_history(self) -> None:
        assert merge_verdict_history(Ticket.objects.create(overlay="t3-teatree")) == ""

    def test_every_merge_verdict_is_listed_oldest_first_and_other_transitions_are_not(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree")
        _merge_verdict(ticket, head="a" * 40, citation="first round")
        _merge_verdict(ticket, head="b" * 40, citation="second round")
        _merge_verdict(ticket, head="c" * 40, citation="other", transition="mark_delivered")

        history = merge_verdict_history(ticket)

        assert history.index("first round") < history.index("second round")
        assert f"{'a' * 40} test_value: fail; first round" in history
        assert "other" not in history

    def test_an_over_long_history_keeps_the_newest_verdicts(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree")
        for round_number in range(40):
            _merge_verdict(ticket, head=f"{round_number:040x}", citation=f"round-{round_number:02d} " + "x" * 400)

        history = merge_verdict_history(ticket)

        assert "round-39" in history
        assert "round-00" not in history
        assert len(history) < 8000
