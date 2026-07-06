"""Parallel cross-worktree provisioning (souliane/teatree#2949)."""

import sys
import threading
import time
from unittest.mock import MagicMock, patch

from django.test import TestCase

from teatree.core.gates.provision_admission_gate import ProvisionAdmissionVerdict
from teatree.core.management.commands._workspace.provision_parallel import (
    WorktreeProvisionResult,
    provision_worktree_subprocess,
    render_worktree_report,
    run_worktree_provisions_in_parallel,
)
from teatree.core.models import Ticket, Worktree
from teatree.utils.run import TimeoutExpired


class TestRunWorktreeProvisionsInParallel(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/1")

    def _worktrees(self, *repos: str) -> list[Worktree]:
        return [
            Worktree.objects.create(
                ticket=self.ticket, repo_path=repo, branch="b", extra={"worktree_path": f"/tmp/{repo}"}
            )
            for repo in repos
        ]

    def test_empty_list_returns_empty(self) -> None:
        results = run_worktree_provisions_in_parallel([], executor=lambda wt: None)
        assert results == []

    def test_runs_every_worktree_and_preserves_order(self) -> None:
        worktrees = self._worktrees("a", "b", "c")

        def fake_executor(wt: Worktree) -> WorktreeProvisionResult:
            return WorktreeProvisionResult(worktree_id=wt.pk, repo_path=wt.repo_path, ok=True, detail="ok")

        results = run_worktree_provisions_in_parallel(
            worktrees,
            executor=fake_executor,
            max_workers=3,
            admission_check=ProvisionAdmissionVerdict.allow,
        )
        assert [r.repo_path for r in results] == ["a", "b", "c"]
        assert all(r.ok for r in results)

    def test_two_worktrees_run_concurrently_under_a_bounded_pool(self) -> None:
        """ANTI-VACUITY: revert to serial dispatch and this deadlocks/times out.

        Two executors gated on the SAME barrier can only both complete if
        both were submitted before either returns — i.e. they ran concurrently.
        """
        worktrees = self._worktrees("a", "b")
        barrier = threading.Barrier(2, timeout=5)

        def fake_executor(wt: Worktree) -> WorktreeProvisionResult:
            barrier.wait()
            return WorktreeProvisionResult(worktree_id=wt.pk, repo_path=wt.repo_path, ok=True, detail="ok")

        results = run_worktree_provisions_in_parallel(
            worktrees,
            executor=fake_executor,
            max_workers=2,
            admission_check=ProvisionAdmissionVerdict.allow,
        )
        assert len(results) == 2
        assert all(r.ok for r in results)

    def test_respects_max_workers_cap(self) -> None:
        worktrees = self._worktrees("a", "b", "c", "d")
        concurrent_count = 0
        max_seen = 0
        lock = threading.Lock()

        def fake_executor(wt: Worktree) -> WorktreeProvisionResult:
            nonlocal concurrent_count, max_seen
            with lock:
                concurrent_count += 1
                max_seen = max(max_seen, concurrent_count)
            time.sleep(0.05)
            with lock:
                concurrent_count -= 1
            return WorktreeProvisionResult(worktree_id=wt.pk, repo_path=wt.repo_path, ok=True, detail="ok")

        run_worktree_provisions_in_parallel(
            worktrees,
            executor=fake_executor,
            max_workers=2,
            admission_check=ProvisionAdmissionVerdict.allow,
            poll_interval=0.01,
        )
        assert max_seen <= 2

    def test_held_admission_delays_submission_then_drains(self) -> None:
        """RAM-held requests are not started; they drain once admission allows."""
        worktrees = self._worktrees("a")
        calls = {"admission": 0}

        def flaky_admission() -> ProvisionAdmissionVerdict:
            calls["admission"] += 1
            if calls["admission"] < 3:
                return ProvisionAdmissionVerdict.hold("ram_pressure (used=99% >= ceiling=85%)")
            return ProvisionAdmissionVerdict.allow()

        executed: list[str] = []

        def fake_executor(wt: Worktree) -> WorktreeProvisionResult:
            executed.append(wt.repo_path)
            return WorktreeProvisionResult(worktree_id=wt.pk, repo_path=wt.repo_path, ok=True, detail="ok")

        results = run_worktree_provisions_in_parallel(
            worktrees,
            executor=fake_executor,
            max_workers=1,
            admission_check=flaky_admission,
            sleep=lambda _s: None,
            poll_interval=0.0,
        )
        assert executed == ["a"]
        assert results[0].ok is True
        assert calls["admission"] >= 3

    def test_hold_past_max_seconds_is_overridden_not_infinite(self) -> None:
        """ANTI-VACUITY: without the override, this test hangs until the real pytest timeout kills it."""
        worktrees = self._worktrees("a")
        clock = {"t": 0.0}

        def advancing_now() -> float:
            clock["t"] += 10.0
            return clock["t"]

        executed: list[str] = []

        def fake_executor(wt: Worktree) -> WorktreeProvisionResult:
            executed.append(wt.repo_path)
            return WorktreeProvisionResult(worktree_id=wt.pk, repo_path=wt.repo_path, ok=True, detail="ok")

        results = run_worktree_provisions_in_parallel(
            worktrees,
            executor=fake_executor,
            max_workers=1,
            admission_check=lambda: ProvisionAdmissionVerdict.hold("ram_pressure (used=99% >= ceiling=85%)"),
            sleep=lambda _s: None,
            poll_interval=0.0,
            max_hold_seconds=30.0,
            now=advancing_now,
        )
        assert executed == ["a"]
        assert results[0].ok is True

    def test_failure_in_one_does_not_abort_others(self) -> None:
        worktrees = self._worktrees("a", "b")

        def fake_executor(wt: Worktree) -> WorktreeProvisionResult:
            ok = wt.repo_path != "a"
            return WorktreeProvisionResult(worktree_id=wt.pk, repo_path=wt.repo_path, ok=ok, detail="x")

        results = run_worktree_provisions_in_parallel(
            worktrees,
            executor=fake_executor,
            max_workers=2,
            admission_check=ProvisionAdmissionVerdict.allow,
        )
        by_repo = {r.repo_path: r.ok for r in results}
        assert by_repo == {"a": False, "b": True}

    def test_writer_receives_progress_lines(self) -> None:
        worktrees = self._worktrees("a")
        lines: list[str] = []

        def fake_executor(wt: Worktree) -> WorktreeProvisionResult:
            return WorktreeProvisionResult(worktree_id=wt.pk, repo_path=wt.repo_path, ok=True, detail="done")

        run_worktree_provisions_in_parallel(
            worktrees,
            executor=fake_executor,
            max_workers=1,
            admission_check=ProvisionAdmissionVerdict.allow,
            write=lines.append,
        )
        assert any("Provisioning a" in line for line in lines)
        assert any("done" in line for line in lines)


class TestProvisionWorktreeSubprocess(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/2")
        self.worktree = Worktree.objects.create(
            ticket=self.ticket, repo_path="backend", branch="b", extra={"worktree_path": "/tmp/backend"}
        )

    def test_builds_the_expected_command(self) -> None:
        with patch("teatree.core.management.commands._workspace.provision_parallel.run_allowed_to_fail") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="  9 step(s) ok\n", stderr="")
            result = provision_worktree_subprocess(self.worktree, overlay_name="test", slow_import=False)
        cmd = mock_run.call_args.args[0]
        assert cmd == [sys.executable, "-m", "teatree", "worktree", "provision", "--path", "/tmp/backend"]
        env = mock_run.call_args.kwargs["env"]
        assert env["T3_OVERLAY_NAME"] == "test"
        assert env["DJANGO_SETTINGS_MODULE"] == "teatree.settings"
        assert result.ok is True
        assert result.detail == "9 step(s) ok"

    def test_slow_import_flag_appended(self) -> None:
        with patch("teatree.core.management.commands._workspace.provision_parallel.run_allowed_to_fail") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="ok\n", stderr="")
            provision_worktree_subprocess(self.worktree, overlay_name="test", slow_import=True)
        cmd = mock_run.call_args.args[0]
        assert cmd[-1] == "--slow-import"

    def test_nonzero_exit_is_not_ok(self) -> None:
        with patch("teatree.core.management.commands._workspace.provision_parallel.run_allowed_to_fail") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Provision failed for backend\n")
            result = provision_worktree_subprocess(self.worktree, overlay_name="test", slow_import=False)
        assert result.ok is False
        assert "Provision failed for backend" in result.detail

    def test_timeout_is_reported_as_failure(self) -> None:
        with patch("teatree.core.management.commands._workspace.provision_parallel.run_allowed_to_fail") as mock_run:
            mock_run.side_effect = TimeoutExpired(cmd=["t3"], timeout=1)
            result = provision_worktree_subprocess(self.worktree, overlay_name="test", slow_import=False, timeout=1)
        assert result.ok is False
        assert "timed out" in result.detail


class TestRenderWorktreeReport(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/3")

    def test_no_report_recorded_yet(self) -> None:
        worktree = Worktree.objects.create(ticket=self.ticket, repo_path="backend", branch="b", extra={})
        rendered = render_worktree_report(worktree)
        assert "no provision report recorded" in rendered
        assert "backend" in rendered

    def test_renders_step_summary_table(self) -> None:
        worktree = Worktree.objects.create(
            ticket=self.ticket,
            repo_path="backend",
            branch="b",
            extra={
                "provision_report": {
                    "success": True,
                    "total_duration": 3.0,
                    "steps": [
                        {
                            "name": "a",
                            "success": True,
                            "duration": 3.0,
                            "error": "",
                            "required": True,
                            "skipped": False,
                        },
                    ],
                }
            },
        )
        rendered = render_worktree_report(worktree)
        assert "backend" in rendered
        assert "[OK] a" in rendered
        assert "1/1 steps succeeded" in rendered
