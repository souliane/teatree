"""Checkout owner cards show the evidence that their approval binds."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.cleanup.process_table import ProcessTable
from teatree.core.worktree.checkout_attest import AttestRefusedError, CheckoutAttestation, attest_card, read_attestation
from teatree.core.worktree.checkout_state import CheckoutState, checkout_state
from teatree.core.worktree.occupancy import acquire
from tests.factories import TaskFactory, TicketFactory, WorktreeFactory
from tests.teatree_core.models._shared import _init_repo_with_branch


class TestCheckoutAttestCard(TestCase):
    def test_card_names_checkout_counts_and_missing_process_evidence(self) -> None:
        ticket = TicketFactory(issue_url="https://github.com/example/repo/issues/17")
        task = TaskFactory(ticket=ticket)
        worktree = WorktreeFactory(ticket=ticket)
        attestation = CheckoutAttestation(
            worktree,
            CheckoutState("d" * 64, commits_ahead=3, uncommitted=2),
            host_evidence=False,
        )
        question, card = attest_card(task, attestation)
        assert card.problems(question) == []
        assert "3 commits" in card.why
        assert "2 uncommitted files" in card.why
        assert "host process list is unavailable" in card.why
        assert card.to_evidence()["action"]["binding"] == "d" * 64

    def test_long_legacy_title_keeps_the_card_answerable(self) -> None:
        ticket = TicketFactory(
            issue_url="https://github.com/example/repo/issues/17",
            short_description="",
            extra={"issue_title": "A checkout title that is much too long " * 8},
        )
        task = TaskFactory(ticket=ticket)
        worktree = WorktreeFactory(ticket=ticket)
        attestation = CheckoutAttestation(
            worktree, CheckoutState("d" * 64, commits_ahead=1, uncommitted=2), host_evidence=True
        )

        question, card = attest_card(task, attestation)

        assert card.problems(question) == []
        assert "A checkout title" in question


class TestReadAttestation(TestCase):
    def setUp(self) -> None:
        super().setUp()
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.checkout = Path(temporary.name) / "checkout"
        _init_repo_with_branch(self.checkout, branch="feature", commits_ahead=0)
        self.worktree = WorktreeFactory(extra={"worktree_path": str(self.checkout)})

    def _read(self, table: ProcessTable, *, expected_digest: str = "") -> CheckoutAttestation:
        with patch("teatree.core.worktree.checkout_attest.read_process_table", return_value=table):
            return read_attestation(self.worktree, expected_digest=expected_digest)

    def test_a_busy_or_occupied_checkout_is_worth_retrying(self) -> None:
        with pytest.raises(AttestRefusedError, match="live or unverifiable processes") as busy:
            self._read(ProcessTable(frozenset({self.checkout}), "/proc"))
        acquire(self.worktree, holder="task:7", holder_session="session-A")
        with pytest.raises(AttestRefusedError, match="unoccupied") as occupied:
            self._read(ProcessTable(frozenset(), "/proc"))

        assert busy.value.retryable
        assert occupied.value.retryable

    def test_a_checkout_that_moved_since_the_question_is_final(self) -> None:
        with pytest.raises(AttestRefusedError, match="changed since the owner was asked") as moved:
            self._read(ProcessTable(frozenset(), "/proc"), expected_digest="0" * 64)

        assert not moved.value.retryable

    def test_a_matching_checkout_reads_without_writing(self) -> None:
        digest = checkout_state(self.checkout).digest

        attestation = self._read(ProcessTable(frozenset(), "/proc"), expected_digest=digest)

        assert attestation.recorded == digest
        self.worktree.refresh_from_db()
        assert "attested_state" not in self.worktree.get_extra()
