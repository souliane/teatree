"""Per-worktree FSM task workers — state guard, runner dispatch, result shape."""

from collections.abc import Iterator
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.models import Ticket, Worktree
from teatree.core.runners.base import RunnerResult
from teatree.core.worktree_tasks import (
    execute_worktree_provision,
    execute_worktree_start,
    execute_worktree_teardown,
    execute_worktree_verify,
)
from tests.teatree_core.conftest import CommandOverlay


@pytest.fixture(autouse=True)
def _no_op_signals() -> None:
    """Suppress on_commit task dispatch — we call workers directly."""
    return


class _WorktreeTaskTest(TestCase):
    @pytest.fixture(autouse=True)
    def _register_test_overlay(self) -> Iterator[None]:
        # The provision worker poison-guards on overlay resolvability
        # (souliane/teatree#1975); register ``test`` so the dispatch path
        # is exercised rather than short-circuited as an unknown overlay.
        with patch("teatree.core.overlay_loader._discover_overlays", return_value={"test": CommandOverlay()}):
            yield

    def _ticket(self) -> Ticket:
        return Ticket.objects.create(overlay="test", issue_url="https://example.com/1")

    def _worktree(self, *, state: Worktree.State = Worktree.State.CREATED) -> Worktree:
        return Worktree.objects.create(
            ticket=self._ticket(),
            overlay="test",
            repo_path="repo",
            branch="feat-x",
            state=state,
            extra={"worktree_path": "/tmp/wt"},
        )


class TestExecuteWorktreeProvision(_WorktreeTaskTest):
    def test_skips_when_state_is_not_provisioned(self) -> None:
        wt = self._worktree(state=Worktree.State.CREATED)
        with patch("teatree.core.worktree_tasks.WorktreeProvisionRunner") as runner:
            result = execute_worktree_provision.call(wt.pk)
        assert result == {"worktree_id": wt.pk, "skipped": True, "state": "created"}
        runner.assert_not_called()

    def test_returns_ok_when_runner_succeeds(self) -> None:
        wt = self._worktree(state=Worktree.State.PROVISIONED)
        with patch("teatree.core.worktree_tasks.WorktreeProvisionRunner") as runner:
            runner.return_value.run.return_value = RunnerResult(ok=True, detail="done")
            result = execute_worktree_provision.call(wt.pk)
        assert result == {"worktree_id": wt.pk, "ok": True, "detail": "done"}

    def test_returns_failure_when_runner_fails(self) -> None:
        wt = self._worktree(state=Worktree.State.PROVISIONED)
        with patch("teatree.core.worktree_tasks.WorktreeProvisionRunner") as runner:
            runner.return_value.run.return_value = RunnerResult(ok=False, detail="boom")
            result = execute_worktree_provision.call(wt.pk)
        assert result == {"worktree_id": wt.pk, "ok": False, "detail": "boom"}


class TestExecuteWorktreeStart(_WorktreeTaskTest):
    def test_skips_when_state_is_not_services_up(self) -> None:
        wt = self._worktree(state=Worktree.State.PROVISIONED)
        with patch("teatree.core.worktree_tasks.WorktreeStartRunner") as runner:
            result = execute_worktree_start.call(wt.pk)
        assert result["skipped"] is True
        runner.assert_not_called()

    def test_returns_ok_when_runner_succeeds(self) -> None:
        wt = self._worktree(state=Worktree.State.SERVICES_UP)
        with patch("teatree.core.worktree_tasks.WorktreeStartRunner") as runner:
            runner.return_value.run.return_value = RunnerResult(ok=True, detail="up")
            result = execute_worktree_start.call(wt.pk)
        assert result == {"worktree_id": wt.pk, "ok": True, "detail": "up"}

    def test_returns_failure_when_runner_fails(self) -> None:
        wt = self._worktree(state=Worktree.State.SERVICES_UP)
        with patch("teatree.core.worktree_tasks.WorktreeStartRunner") as runner:
            runner.return_value.run.return_value = RunnerResult(ok=False, detail="docker-error")
            result = execute_worktree_start.call(wt.pk)
        assert result == {"worktree_id": wt.pk, "ok": False, "detail": "docker-error"}


class TestExecuteWorktreeVerify(_WorktreeTaskTest):
    def test_skips_when_state_is_not_ready(self) -> None:
        wt = self._worktree(state=Worktree.State.SERVICES_UP)
        with patch("teatree.core.worktree_tasks.WorktreeVerifyRunner") as runner:
            result = execute_worktree_verify.call(wt.pk)
        assert result["skipped"] is True
        runner.assert_not_called()

    def test_returns_ok_when_runner_succeeds(self) -> None:
        wt = self._worktree(state=Worktree.State.READY)
        with patch("teatree.core.worktree_tasks.WorktreeVerifyRunner") as runner:
            runner.return_value.run.return_value = RunnerResult(ok=True, detail="healthy")
            result = execute_worktree_verify.call(wt.pk)
        assert result == {"worktree_id": wt.pk, "ok": True, "detail": "healthy"}

    def test_returns_failure_when_runner_reports_problems(self) -> None:
        wt = self._worktree(state=Worktree.State.READY)
        with patch("teatree.core.worktree_tasks.WorktreeVerifyRunner") as runner:
            runner.return_value.run.return_value = RunnerResult(ok=False, detail="sick")
            result = execute_worktree_verify.call(wt.pk)
        assert result == {"worktree_id": wt.pk, "ok": False, "detail": "sick"}


class TestExecuteWorktreeTeardown(_WorktreeTaskTest):
    def test_no_ops_when_worktree_row_already_gone(self) -> None:
        with patch("teatree.core.worktree_tasks.WorktreeTeardownRunner") as runner:
            result = execute_worktree_teardown.call(999_999, snapshot_db_name="db", snapshot_extra={})
        assert result == {"worktree_id": 999_999, "skipped": True}
        runner.assert_not_called()

    def test_returns_ok_when_teardown_runner_succeeds(self) -> None:
        wt = self._worktree(state=Worktree.State.CREATED)
        with patch("teatree.core.worktree_tasks.WorktreeTeardownRunner") as runner:
            runner.return_value.run.return_value = RunnerResult(ok=True, detail="cleaned")
            result = execute_worktree_teardown.call(wt.pk, snapshot_db_name="db_old", snapshot_extra={})
        assert result == {"worktree_id": wt.pk, "ok": True, "detail": "cleaned"}

    def test_returns_failure_when_teardown_runner_fails(self) -> None:
        wt = self._worktree(state=Worktree.State.CREATED)
        with patch("teatree.core.worktree_tasks.WorktreeTeardownRunner") as runner:
            runner.return_value.run.return_value = RunnerResult(ok=False, detail="docker stuck")
            result = execute_worktree_teardown.call(wt.pk, snapshot_db_name="db_old", snapshot_extra={})
        assert result == {"worktree_id": wt.pk, "ok": False, "detail": "docker stuck"}
