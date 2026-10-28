"""Direct owner attestation rechecks the live checkout."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.cleanup.process_table import ProcessTable
from teatree.core.worktree.checkout_attest import AttestRefusedError, CheckoutAttestation, read_attestation
from teatree.core.worktree.checkout_state import checkout_state
from teatree.core.worktree.occupancy import acquire
from tests.factories import WorktreeFactory
from tests.teatree_core.models._shared import _init_repo_with_branch


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

    def test_unreadable_same_user_process_does_not_count_as_host_evidence(self) -> None:
        table = ProcessTable(frozenset(), "/host-proc", (), ("pid 7 unreadable",))
        attestation = self._read(table)

        assert not attestation.host_evidence
        assert attestation.recorded.startswith("owner:")
        with (
            patch("teatree.core.worktree.checkout_attest.read_process_table", return_value=table),
            pytest.raises(AttestRefusedError, match="cannot rule out live processes") as error,
        ):
            read_attestation(self.worktree, requested_host_evidence=True)
        assert error.value.retryable

    def test_unreadable_git_state_is_retryable(self) -> None:
        with (
            patch("teatree.core.worktree.checkout_attest.checkout_state", side_effect=OSError("transient")),
            pytest.raises(AttestRefusedError, match="state could not be read") as error,
        ):
            self._read(ProcessTable(frozenset(), "/proc"))

        assert error.value.retryable
