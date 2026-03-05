"""Tests for teatree/scripts/wt_finalize.py."""

from types import SimpleNamespace
from unittest.mock import patch

from conftest import load_script, run_ok


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(
        wt_dir="/tmp/wt/repo",
        main_repo="/tmp/main/repo",
        ticket_dir="/tmp/wt",
        ticket_number="123",
    )


class TestWtFinalize:
    def test_context_resolution_failure(self) -> None:
        mod = load_script("wt_finalize")
        with patch.object(mod, "resolve_context", side_effect=RuntimeError("bad")):
            assert mod.wt_finalize() == 1

    def test_default_branch_failure(self) -> None:
        mod = load_script("wt_finalize")
        with (
            patch.object(mod, "resolve_context", return_value=_ctx()),
            patch.object(mod, "default_branch", side_effect=RuntimeError("no default")),
        ):
            assert mod.wt_finalize() == 1

    def test_uncommitted_changes_blocked(self) -> None:
        mod = load_script("wt_finalize")
        with (
            patch.object(mod, "resolve_context", return_value=_ctx()),
            patch.object(mod, "default_branch", return_value="master"),
            patch.object(mod, "subprocess") as mock_sp,
        ):
            mock_sp.run.side_effect = [
                run_ok(returncode=1),  # git diff --quiet (dirty)
                run_ok(),  # git diff --cached
            ]
            assert mod.wt_finalize() == 1

    def test_no_commits_to_squash(self) -> None:
        mod = load_script("wt_finalize")
        with (
            patch.object(mod, "resolve_context", return_value=_ctx()),
            patch.object(mod, "default_branch", return_value="master"),
            patch.object(mod, "subprocess") as mock_sp,
        ):
            mock_sp.run.side_effect = [
                run_ok(),  # diff --quiet
                run_ok(),  # diff --cached --quiet
                run_ok(),  # fetch origin
                run_ok(),  # pull main
                run_ok(stdout="abc123\n"),  # merge-base
                run_ok(stdout="0\n"),  # rev-list --count
            ]
            assert mod.wt_finalize() == 0

    def test_pull_failure_warns_but_continues(self) -> None:
        """Covers: line 57 (pull fails, warning printed, continues)."""
        mod = load_script("wt_finalize")
        with (
            patch.object(mod, "resolve_context", return_value=_ctx()),
            patch.object(mod, "default_branch", return_value="master"),
            patch.object(mod, "subprocess") as mock_sp,
        ):
            mock_sp.run.side_effect = [
                run_ok(),  # diff --quiet
                run_ok(),  # diff --cached --quiet
                run_ok(),  # fetch
                run_ok(returncode=1),  # pull FAILS -> line 57
                run_ok(stdout="fork123\n"),  # merge-base
                run_ok(stdout="0\n"),  # rev-list --count
            ]
            assert mod.wt_finalize() == 0

    def test_rebase_conflict_returns_1(self) -> None:
        mod = load_script("wt_finalize")
        with (
            patch.object(mod, "resolve_context", return_value=_ctx()),
            patch.object(mod, "default_branch", return_value="master"),
            patch.object(mod, "subprocess") as mock_sp,
        ):
            mock_sp.run.side_effect = [
                run_ok(),  # diff --quiet
                run_ok(),  # diff --cached --quiet
                run_ok(),  # fetch
                run_ok(),  # pull main
                run_ok(stdout="fork123\n"),  # merge-base
                run_ok(stdout="1\n"),  # rev-list --count
                run_ok(stdout="my-branch\n"),  # branch --show-current
                run_ok(),  # git log --oneline
                run_ok(stdout="fix: conflict\n"),  # git log --format=%s
                run_ok(),  # git reset --soft
                run_ok(),  # git commit -m
                run_ok(returncode=1),  # git rebase fails
            ]
            assert mod.wt_finalize() == 1
