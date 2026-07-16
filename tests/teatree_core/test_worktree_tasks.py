"""Per-worktree FSM task workers — state guard, runner dispatch, result shape."""

from collections.abc import Iterator
from unittest.mock import patch

import pytest
from django.db import connection
from django.test import TestCase, TransactionTestCase

from teatree.core.models import Ticket, Worktree
from teatree.core.runners.base import RunnerResult
from teatree.core.worktree.worktree_tasks import (
    execute_worktree_provision,
    execute_worktree_start,
    execute_worktree_stop,
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

    def _unknown_overlay_worktree(self, *, state: Worktree.State) -> Worktree:
        """A worktree whose overlay is no longer registered (uninstalled/foreign).

        ``resolve_overlay_name("ghost-overlay")`` returns ``None`` under the
        ``{"test": CommandOverlay()}`` registry, so a worker that constructs its
        runner would call ``get_overlay_for_worktree`` and raise ``Overlay not
        found`` on every re-fire.
        """
        ticket = Ticket.objects.create(overlay="ghost-overlay", issue_url="https://example.com/ghost")
        return Worktree.objects.create(
            ticket=ticket,
            overlay="ghost-overlay",
            repo_path="repo",
            branch="feat-x",
            state=state,
            extra={"worktree_path": "/tmp/wt"},
        )


class TestExecuteWorktreeProvision(_WorktreeTaskTest):
    def test_skips_when_state_is_not_provisioned(self) -> None:
        wt = self._worktree(state=Worktree.State.CREATED)
        with patch("teatree.core.worktree.worktree_tasks.WorktreeProvisionRunner") as runner:
            result = execute_worktree_provision.call(wt.pk)
        assert result == {"worktree_id": wt.pk, "skipped": True, "state": "created"}
        runner.assert_not_called()

    def test_returns_ok_when_runner_succeeds(self) -> None:
        wt = self._worktree(state=Worktree.State.PROVISIONED)
        with patch("teatree.core.worktree.worktree_tasks.WorktreeProvisionRunner") as runner:
            runner.return_value.run.return_value = RunnerResult(ok=True, detail="done")
            result = execute_worktree_provision.call(wt.pk)
        assert result == {"worktree_id": wt.pk, "ok": True, "detail": "done"}

    def test_returns_failure_when_runner_fails(self) -> None:
        wt = self._worktree(state=Worktree.State.PROVISIONED)
        with patch("teatree.core.worktree.worktree_tasks.WorktreeProvisionRunner") as runner:
            runner.return_value.run.return_value = RunnerResult(ok=False, detail="boom")
            result = execute_worktree_provision.call(wt.pk)
        assert result == {"worktree_id": wt.pk, "ok": False, "detail": "boom"}


class TestExecuteWorktreeStart(_WorktreeTaskTest):
    def test_skips_when_state_is_not_services_up(self) -> None:
        wt = self._worktree(state=Worktree.State.PROVISIONED)
        with patch("teatree.core.worktree.worktree_tasks.WorktreeStartRunner") as runner:
            result = execute_worktree_start.call(wt.pk)
        assert result["skipped"] is True
        runner.assert_not_called()

    def test_returns_ok_when_runner_succeeds(self) -> None:
        wt = self._worktree(state=Worktree.State.SERVICES_UP)
        with patch("teatree.core.worktree.worktree_tasks.WorktreeStartRunner") as runner:
            runner.return_value.run.return_value = RunnerResult(ok=True, detail="up")
            result = execute_worktree_start.call(wt.pk)
        assert result == {"worktree_id": wt.pk, "ok": True, "detail": "up"}

    def test_returns_failure_when_runner_fails(self) -> None:
        wt = self._worktree(state=Worktree.State.SERVICES_UP)
        with patch("teatree.core.worktree.worktree_tasks.WorktreeStartRunner") as runner:
            runner.return_value.run.return_value = RunnerResult(ok=False, detail="docker-error")
            result = execute_worktree_start.call(wt.pk)
        assert result == {"worktree_id": wt.pk, "ok": False, "detail": "docker-error"}

    def test_unknown_overlay_fails_permanently_not_raises(self) -> None:
        """An unregistered overlay degrades to a recorded ``ok=False`` — never raises.

        Parity with ``execute_worktree_provision``'s poison-pill guard
        (souliane/teatree#1975). The real start runner resolves the overlay in
        its ``__init__`` (``get_overlay_for_worktree``), which raises ``Overlay
        not found`` on every re-fire for a worktree whose overlay was
        uninstalled. The runner is left UNMOCKED so the test reproduces the
        actual production raise; the worker must short-circuit BEFORE
        constructing the runner so one bad worktree never crashes its FSM
        worker forever.
        """
        wt = self._unknown_overlay_worktree(state=Worktree.State.SERVICES_UP)
        result = execute_worktree_start.call(wt.pk)
        assert result["worktree_id"] == wt.pk
        assert result["ok"] is False
        assert "ghost-overlay" in result["detail"]


class TestExecuteWorktreeVerify(_WorktreeTaskTest):
    def test_skips_when_state_is_not_ready(self) -> None:
        wt = self._worktree(state=Worktree.State.SERVICES_UP)
        with patch("teatree.core.worktree.worktree_tasks.WorktreeVerifyRunner") as runner:
            result = execute_worktree_verify.call(wt.pk)
        assert result["skipped"] is True
        runner.assert_not_called()

    def test_returns_ok_when_runner_succeeds(self) -> None:
        wt = self._worktree(state=Worktree.State.READY)
        with patch("teatree.core.worktree.worktree_tasks.WorktreeVerifyRunner") as runner:
            runner.return_value.run.return_value = RunnerResult(ok=True, detail="healthy")
            result = execute_worktree_verify.call(wt.pk)
        assert result == {"worktree_id": wt.pk, "ok": True, "detail": "healthy"}

    def test_returns_failure_when_runner_reports_problems(self) -> None:
        wt = self._worktree(state=Worktree.State.READY)
        with patch("teatree.core.worktree.worktree_tasks.WorktreeVerifyRunner") as runner:
            runner.return_value.run.return_value = RunnerResult(ok=False, detail="sick")
            result = execute_worktree_verify.call(wt.pk)
        assert result == {"worktree_id": wt.pk, "ok": False, "detail": "sick"}

    def test_unknown_overlay_fails_permanently_not_raises(self) -> None:
        """An unregistered overlay degrades to a recorded ``ok=False`` — never raises.

        Parity with ``execute_worktree_provision``'s poison-pill guard
        (souliane/teatree#1975). The real verify runner resolves the overlay in
        its ``__init__`` (``get_overlay_for_worktree``), which raises ``Overlay
        not found`` on every re-fire for a worktree whose overlay was
        uninstalled. The runner is left UNMOCKED so the test reproduces the
        actual production raise; the worker must short-circuit BEFORE
        constructing the runner.
        """
        wt = self._unknown_overlay_worktree(state=Worktree.State.READY)
        result = execute_worktree_verify.call(wt.pk)
        assert result["worktree_id"] == wt.pk
        assert result["ok"] is False
        assert "ghost-overlay" in result["detail"]


class TestExecuteWorktreeTeardown(_WorktreeTaskTest):
    def test_no_ops_when_worktree_row_already_gone(self) -> None:
        with patch("teatree.core.worktree.worktree_tasks.WorktreeTeardownRunner") as runner:
            result = execute_worktree_teardown.call(999_999, snapshot_db_name="db", snapshot_extra={})
        assert result == {"worktree_id": 999_999, "skipped": True}
        runner.assert_not_called()

    def test_returns_ok_when_teardown_runner_succeeds(self) -> None:
        wt = self._worktree(state=Worktree.State.CREATED)
        with patch("teatree.core.worktree.worktree_tasks.WorktreeTeardownRunner") as runner:
            runner.return_value.run.return_value = RunnerResult(ok=True, detail="cleaned")
            result = execute_worktree_teardown.call(wt.pk, snapshot_db_name="db_old", snapshot_extra={})
        assert result == {"worktree_id": wt.pk, "ok": True, "detail": "cleaned"}

    def test_returns_failure_when_teardown_runner_fails(self) -> None:
        wt = self._worktree(state=Worktree.State.CREATED)
        with patch("teatree.core.worktree.worktree_tasks.WorktreeTeardownRunner") as runner:
            runner.return_value.run.return_value = RunnerResult(ok=False, detail="docker stuck")
            result = execute_worktree_teardown.call(wt.pk, snapshot_db_name="db_old", snapshot_extra={})
        assert result == {"worktree_id": wt.pk, "ok": False, "detail": "docker stuck"}


class TestExecuteWorktreeStop(_WorktreeTaskTest):
    """``execute_worktree_stop`` brings the WHOLE compose project down (reversible)."""

    def test_skips_when_state_is_not_provisioned(self) -> None:
        """The transition demotes to PROVISIONED first; a non-PROVISIONED row is a stale read."""
        wt = self._worktree(state=Worktree.State.SERVICES_UP)
        with patch("teatree.core.worktree.worktree_tasks.docker_compose_down") as down:
            result = execute_worktree_stop.call(wt.pk)
        assert result["skipped"] is True
        down.assert_not_called()

    def test_brings_the_whole_compose_project_down(self) -> None:
        from teatree.core.worktree.worktree_env import compose_project  # noqa: PLC0415

        wt = self._worktree(state=Worktree.State.PROVISIONED)
        expected_project = compose_project(wt)
        with patch("teatree.core.worktree.worktree_tasks.docker_compose_down") as down:
            result = execute_worktree_stop.call(wt.pk)
        assert result["worktree_id"] == wt.pk
        assert result["ok"] is True
        # The whole project (all containers incl db), never a single service.
        down.assert_called_once()
        assert down.call_args.args[0] == expected_project

    def test_no_ops_when_worktree_row_already_gone(self) -> None:
        with patch("teatree.core.worktree.worktree_tasks.docker_compose_down") as down:
            result = execute_worktree_stop.call(999_999)
        assert result == {"worktree_id": 999_999, "skipped": True}
        down.assert_not_called()

    def test_preserves_db_name(self) -> None:
        """REVERSIBLE: the worker must not drop the DB (unlike teardown)."""
        wt = self._worktree(state=Worktree.State.PROVISIONED)
        wt.db_name = "wt_keepme"
        wt.save(update_fields=["db_name"])
        with patch("teatree.core.worktree.worktree_tasks.docker_compose_down"):
            execute_worktree_stop.call(wt.pk)
        wt.refresh_from_db()
        assert wt.db_name == "wt_keepme"


class TestWorkerRunsRunnerOutsideClaimLock(TransactionTestCase):
    """The heavy runner runs OUTSIDE the short claim transaction (SQLite lock fix).

    The FSM workers used to wrap the minutes-long runner (``uv sync`` / DB import
    / ``docker compose up`` / health checks) inside ``atomic() +
    select_for_update``. On the SQLite control DB that held the connection-level
    write lock for the whole duration, freezing every other worker ("database is
    locked"). The claim now holds a SHORT lock (state re-check) and releases it
    before the runner runs — proven here by the runner observing that it is NOT
    inside an open transaction (``TransactionTestCase`` so there is no outer
    atomic wrapper to confound the probe).
    """

    def _worktree(self, *, state: Worktree.State) -> Worktree:
        # Blank overlay = ambient single-overlay default, dispatchable with no
        # overlay registration (skips the unknown-overlay poison guard).
        ticket = Ticket.objects.create(overlay="", issue_url="https://example.com/lock")
        return Worktree.objects.create(
            ticket=ticket,
            overlay="",
            repo_path="repo",
            branch="feat-lock",
            state=state,
            extra={"worktree_path": "/tmp/wt"},
        )

    def _spy_runner(self, seen: dict[str, bool]) -> type:
        class _Spy:
            def __init__(self, worktree: Worktree) -> None:
                self.worktree = worktree

            def run(self) -> RunnerResult:
                seen["in_atomic"] = connection.in_atomic_block
                return RunnerResult(ok=True, detail="done")

        return _Spy

    def test_provision_runner_runs_outside_transaction(self) -> None:
        wt = self._worktree(state=Worktree.State.PROVISIONED)
        seen: dict[str, bool] = {}
        with patch(
            "teatree.core.worktree.worktree_tasks.WorktreeProvisionRunner",
            self._spy_runner(seen),
        ):
            result = execute_worktree_provision.call(wt.pk)
        assert result == {"worktree_id": wt.pk, "ok": True, "detail": "done"}
        assert seen["in_atomic"] is False

    def test_start_runner_runs_outside_transaction(self) -> None:
        wt = self._worktree(state=Worktree.State.SERVICES_UP)
        seen: dict[str, bool] = {}
        with patch(
            "teatree.core.worktree.worktree_tasks.WorktreeStartRunner",
            self._spy_runner(seen),
        ):
            execute_worktree_start.call(wt.pk)
        assert seen["in_atomic"] is False

    def test_verify_runner_runs_outside_transaction(self) -> None:
        wt = self._worktree(state=Worktree.State.READY)
        seen: dict[str, bool] = {}
        with patch(
            "teatree.core.worktree.worktree_tasks.WorktreeVerifyRunner",
            self._spy_runner(seen),
        ):
            execute_worktree_verify.call(wt.pk)
        assert seen["in_atomic"] is False
