"""Critic contracts retain head-to-head evidence without treating missing reads as clean."""

from unittest.mock import patch

from django.test import TestCase

from teatree.core.models import CriticVerdict, Ticket
from teatree.core.review.critic_continuity import merge_critic_continuity, review_evidence_contract
from teatree.quality.kill_matrix import KillMatrix, KillRow
from teatree.quality.review_evidence import ReviewEvidence
from teatree.utils.run import CommandFailedError


class TestCriticContinuity(TestCase):
    def test_critic_receives_head_bound_matrix_and_missing_receipt_is_unverified(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEW_REQUESTED)
        head = "a" * 40
        with patch("teatree.core.review.critic_continuity.dispatch_worktree_path", return_value=""):
            assert "UNAVAILABLE" in review_evidence_contract(ticket, head)
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
            assert "return test_value unverified" in review_evidence_contract(ticket, head)

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
