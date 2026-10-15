from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.cleanup.process_table import ProcessTable
from teatree.core.modelkit.task_failure_taxonomy import FailureKind
from teatree.core.models import Task, Worktree
from teatree.core.worktree.checkout_fence import CheckoutFenceError, verify_task_checkout
from teatree.core.worktree.checkout_state import CheckoutState, checkout_state
from teatree.core.worktree.occupancy import acquire, release, task_holder_id
from teatree.mcp.serializers import serialize_worktree
from teatree.utils.git import run_strict
from tests.factories import TaskFactory, TicketFactory, WorktreeFactory
from tests.teatree_core.models._shared import _init_repo_with_branch


class CheckoutRecoveryTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "checkout"
        _init_repo_with_branch(self.path, branch="feature", commits_ahead=0)
        self.ticket = TicketFactory()
        self.previous = TaskFactory(ticket=self.ticket, phase="coding")
        self.next_task = TaskFactory(ticket=self.ticket, phase="testing")
        self.worktree = WorktreeFactory(ticket=self.ticket, branch="feature", extra={"worktree_path": str(self.path)})
        self.clear = ProcessTable(frozenset(), "/proc")
        with patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=self.clear):
            verify_task_checkout(self.previous, str(self.path))
        self.anchor = checkout_state(self.path).digest
        acquire(self.worktree, holder=task_holder_id(self.previous))
        (self.path / "change.txt").write_text("interrupted change")
        run_strict(repo=str(self.path), args=["config", "user.email", "test@example.com"])
        run_strict(repo=str(self.path), args=["config", "user.name", "Test"])
        run_strict(repo=str(self.path), args=["add", "change.txt"])
        run_strict(repo=str(self.path), args=["commit", "-m", "interrupted change"])
        release(self.worktree, holder=task_holder_id(self.previous))

    def dispatch(self, table: ProcessTable | None = None) -> None:
        acquire(self.worktree, holder=task_holder_id(self.next_task))
        with patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=table or self.clear):
            verify_task_checkout(self.next_task, str(self.path))

    def assert_parked(self, table: ProcessTable | None = None) -> None:
        with pytest.raises(CheckoutFenceError):
            self.dispatch(table)
        self.worktree.refresh_from_db()
        assert self.worktree.get_extra()["attested_state"] == self.anchor

    def test_a_hand_holder_between_runs_refuses(self) -> None:
        acquire(self.worktree, holder="operator", holder_session="terminal")
        release(self.worktree, holder="operator", holder_session="terminal")
        self.assert_parked()

    def test_unusable_process_table_refuses(self) -> None:
        self.assert_parked(ProcessTable(frozenset(), "", ("unreadable",)))

    def test_unreadable_same_user_process_refuses(self) -> None:
        self.assert_parked(ProcessTable(frozenset(), "/proc", (), ("pid 7 (python) is unreadable by this uid",)))

    def test_live_process_refuses_then_a_retry_recovers(self) -> None:
        self.assert_parked(ProcessTable(frozenset({self.path}), "/proc"))
        release(self.worktree, holder=task_holder_id(self.next_task))
        self.dispatch()
        self.worktree.refresh_from_db()
        assert self.worktree.get_extra()["attested_state"] == checkout_state(self.path).digest

    def test_wrong_dispatch_branch_refuses(self) -> None:
        run_strict(repo=str(self.path), args=["checkout", "-b", "other"])
        self.assert_parked()

    def test_wrong_sibling_branch_refuses(self) -> None:
        sibling = self.path.parent / "sibling"
        _init_repo_with_branch(sibling, branch="feature", commits_ahead=0)
        WorktreeFactory(ticket=self.ticket, branch="feature", extra={"worktree_path": str(sibling)})
        run_strict(repo=str(sibling), args=["checkout", "-b", "other"])
        self.assert_parked()

    def test_unreadable_sibling_refuses(self) -> None:
        WorktreeFactory(
            ticket=self.ticket, branch="feature", extra={"worktree_path": str(self.path.parent / "missing")}
        )
        self.assert_parked()

    def test_empty_expected_branch_refuses(self) -> None:
        Worktree.objects.filter(pk=self.worktree.pk).update(branch="")
        self.assert_parked()

    def test_state_changed_during_recovery_refuses(self) -> None:
        stale = CheckoutState(digest="different", commits_ahead=1, dirty=False, branch="feature")
        with patch("teatree.core.worktree.checkout_recovery.checkout_state", return_value=stale):
            self.assert_parked()

    def test_unpersisted_attestation_refuses(self) -> None:
        with patch.object(Worktree, "merge_extra", return_value=None):
            self.assert_parked()

    def test_read_only_previous_holder_refuses(self) -> None:
        Task.objects.filter(pk=self.previous.pk).update(phase="reviewing")
        self.assert_parked()

    def test_legacy_attestation_without_custody_refuses(self) -> None:
        raw = dict(self.worktree.extra)
        raw.pop("checkout_custody")
        Worktree.objects.filter(pk=self.worktree.pk).update(extra=raw)
        self.assert_parked()

    def test_cancelled_holder_refuses(self) -> None:
        Task.objects.filter(pk=self.previous.pk).update(status=Task.Status.FAILED, failure_kind=FailureKind.CANCELLED)
        self.assert_parked()

    def test_audit_is_exposed_without_a_page(self) -> None:
        with patch("teatree.core.worktree.checkout_fence.notify_user") as page:
            self.dispatch()
        page.assert_not_called()
        self.worktree.refresh_from_db()
        audit = self.worktree.get_extra()["auto_attestation"]
        assert audit["previous"] == self.anchor
        assert audit["digest"] == checkout_state(self.path).digest
        assert audit["tenures"][0]["holder"] == task_holder_id(self.previous)
        assert audit["process_table"] == {"source": "/proc", "gaps": []}
        assert audit["branches"] == {str(self.path): "feature"}
        assert serialize_worktree(self.worktree)["auto_attestation"] == audit
