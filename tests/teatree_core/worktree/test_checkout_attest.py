"""Checkout owner cards show the evidence that their approval binds."""

from django.test import TestCase

from teatree.core.worktree.checkout_attest import CheckoutAttestation, attest_card
from teatree.core.worktree.checkout_state import CheckoutState
from tests.factories import TaskFactory, TicketFactory, WorktreeFactory


class TestCheckoutAttestCard(TestCase):
    def test_card_names_checkout_counts_and_missing_process_evidence(self) -> None:
        ticket = TicketFactory(issue_url="https://github.com/example/repo/issues/17")
        task = TaskFactory(ticket=ticket)
        worktree = WorktreeFactory(ticket=ticket)
        attestation = CheckoutAttestation(
            worktree,
            CheckoutState("d" * 64, commits_ahead=3, dirty=True, uncommitted=2),
            host_evidence=False,
        )
        question, card = attest_card(task, attestation)
        assert card.problems(question) == []
        assert "3 commits" in card.why
        assert "2 uncommitted files" in card.why
        assert "host process list is unavailable" in card.why
        assert card.to_evidence()["action"]["binding"] == "d" * 64
