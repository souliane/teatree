from datetime import timedelta
from pathlib import Path
from subprocess import TimeoutExpired
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core import forge_pr_probe
from teatree.core.cleanup.process_table import ProcessTable
from teatree.core.forge_pr_probe import PrProbe
from teatree.core.modelkit.task_failure_taxonomy import FailureKind
from teatree.core.models import Task, TaskAttempt, Worktree
from teatree.core.worktree.checkout_fence import CheckoutFenceError, claim_task_checkout_or_park, verify_task_checkout
from teatree.core.worktree.checkout_state import CheckoutState, checkout_state
from teatree.core.worktree.occupancy import acquire, release, task_holder_id
from teatree.mcp.serializers import serialize_worktree
from teatree.utils.git import run_strict
from tests.factories import PullRequestFactory, TaskFactory, TicketFactory, WorktreeFactory
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
        with (
            patch("teatree.core.worktree.checkout_fence.notify_user") as page,
            patch.object(forge_pr_probe, "find_open_pr_for_branch") as probe,
        ):
            self.dispatch()
        page.assert_not_called()
        probe.assert_not_called()
        self.worktree.refresh_from_db()
        audit = self.worktree.get_extra()["auto_attestation"]
        assert audit["previous"] == self.anchor
        assert audit["digest"] == checkout_state(self.path).digest
        assert audit["tenures"][0]["holder"] == task_holder_id(self.previous)
        assert audit["process_table"] == {"source": "/proc", "gaps": []}
        assert audit["branches"] == {str(self.path): "feature"}
        assert audit["pull_requests"] == {}
        assert serialize_worktree(self.worktree)["auto_attestation"] == audit


class ReRootedCheckoutRecoveryTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "checkout"
        _init_repo_with_branch(self.path, branch="feature", commits_ahead=0)
        self.ticket = TicketFactory()
        self.previous = TaskFactory(ticket=self.ticket, phase="coding")
        self.next_task = TaskFactory(ticket=self.ticket, phase="shipping")
        self.worktree = WorktreeFactory(ticket=self.ticket, branch="feature", extra={"worktree_path": str(self.path)})
        self.clear = ProcessTable(frozenset(), "/proc")
        with patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=self.clear):
            verify_task_checkout(self.previous, str(self.path))
        self.worktree.refresh_from_db()
        self.anchor = self.worktree.get_extra()["attested_state"]
        acquire(self.worktree, holder=task_holder_id(self.previous))
        run_strict(repo=str(self.path), args=["checkout", "-b", "rerooted"])
        (self.path / "change.txt").write_text("interrupted change")
        run_strict(repo=str(self.path), args=["config", "user.email", "test@example.com"])
        run_strict(repo=str(self.path), args=["config", "user.name", "Test"])
        run_strict(repo=str(self.path), args=["add", "change.txt"])
        run_strict(repo=str(self.path), args=["commit", "-m", "interrupted change"])
        Worktree.objects.filter(pk=self.worktree.pk).update(occupancy_expires_at=timezone.now() - timedelta(seconds=1))
        self.pr = PullRequestFactory(ticket=self.ticket)

    def claim(self) -> TaskAttempt | None:
        with (
            patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=self.clear),
            patch("teatree.core.worktree.checkout_fence.notify_user"),
        ):
            return claim_task_checkout_or_park(self.next_task)

    def assert_parked(self) -> None:
        attempt = self.claim()
        assert attempt is not None
        assert "checkout_parked:" in attempt.result["summary"]
        assert "differs from its attested state" in attempt.result["summary"]
        self.worktree.refresh_from_db()
        assert self.worktree.get_extra()["attested_state"] == self.anchor

    def test_a_checkout_on_its_pull_requests_head_branch_is_claimed(self) -> None:
        with patch.object(forge_pr_probe, "find_open_pr_for_branch", return_value=PrProbe.found(self.pr.url)) as probe:
            assert self.claim() is None
        probe.assert_called_once_with(str(self.path), "rerooted")
        self.worktree.refresh_from_db()
        audit = self.worktree.get_extra()["auto_attestation"]
        assert self.worktree.get_extra()["attested_state"] == checkout_state(self.path).digest
        assert self.worktree.branch == "feature"
        assert audit["branches"] == {str(self.path): "rerooted"}
        assert audit["pull_requests"] == {str(self.path): self.pr.url}
        assert audit["tenures"][0]["ended"] == "abandoned"

    def test_a_sibling_on_its_pull_requests_head_branch_is_claimed(self) -> None:
        sibling = self.path.parent / "sibling"
        _init_repo_with_branch(sibling, branch="feature", commits_ahead=0)
        WorktreeFactory(ticket=self.ticket, branch="feature", extra={"worktree_path": str(sibling)})
        run_strict(repo=str(sibling), args=["checkout", "-b", "rerooted"])
        with patch.object(forge_pr_probe, "find_open_pr_for_branch", return_value=PrProbe.found(self.pr.url)) as probe:
            assert self.claim() is None
        probe.assert_any_call(str(self.path), "rerooted")
        probe.assert_any_call(str(sibling), "rerooted")
        assert probe.call_count == 2
        self.worktree.refresh_from_db()
        audit = self.worktree.get_extra()["auto_attestation"]
        assert audit["pull_requests"] == {str(self.path): self.pr.url, str(sibling): self.pr.url}

    def test_a_branch_the_ticket_does_not_own_stays_parked(self) -> None:
        foreign = PullRequestFactory(ticket=TicketFactory())
        for answer in (PrProbe.none(), PrProbe.unknown(), PrProbe.found(foreign.url)):
            with (
                self.subTest(answer=answer),
                patch.object(forge_pr_probe, "find_open_pr_for_branch", return_value=answer) as probe,
            ):
                self.assert_parked()
                probe.assert_called_once_with(str(self.path), "rerooted")

    def test_detached_head_never_probes(self) -> None:
        run_strict(repo=str(self.path), args=["checkout", "--detach"])
        with patch.object(forge_pr_probe, "find_open_pr_for_branch") as probe:
            self.assert_parked()
        probe.assert_not_called()

    def test_unparseable_found_url_stays_parked(self) -> None:
        with patch.object(
            forge_pr_probe, "find_open_pr_for_branch", return_value=PrProbe.found("https://example.com/not-a-pr")
        ) as probe:
            self.assert_parked()
        probe.assert_called_once_with(str(self.path), "rerooted")

    def test_probe_timeout_stays_parked(self) -> None:
        run_strict(repo=str(self.path), args=["remote", "add", "origin", "https://github.com/souliane/teatree.git"])
        with (
            patch.object(forge_pr_probe, "forge_cli_env", return_value={}),
            patch.object(forge_pr_probe, "run_allowed_to_fail", side_effect=TimeoutExpired("gh", 30)) as run,
        ):
            self.assert_parked()
        assert run.call_args.kwargs["timeout"] == 30
