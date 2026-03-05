"""Tests for teatree/scripts/ws_ticket.py."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from conftest import load_script, run_ok


class TestWsTicket:
    def test_workspace_not_found(self, tmp_path: Path) -> None:
        mod = load_script("ws_ticket")
        missing = str(tmp_path / "nope")
        with (
            patch.object(mod, "workspace_dir", return_value=missing),
            patch.object(mod, "branch_prefix", return_value="ac"),
        ):
            assert mod.ws_ticket("123", "fix", ["repo1"]) == 1

    def test_skips_non_git_repo(self, tmp_path: Path) -> None:
        mod = load_script("ws_ticket")
        not_a_repo = tmp_path / "not-a-repo"
        not_a_repo.mkdir()
        # no .git inside

        with (
            patch.object(mod, "workspace_dir", return_value=str(tmp_path)),
            patch.object(mod, "branch_prefix", return_value="ac"),
        ):
            result = mod.ws_ticket("123", "fix", ["not-a-repo"])
        assert result == 1

    def test_skips_existing_worktree(self, tmp_path: Path) -> None:
        mod = load_script("ws_ticket")
        repo = tmp_path / "myrepo"
        (repo / ".git").mkdir(parents=True)
        ticket_dir = tmp_path / "ac-myrepo-123-fix"
        wt = ticket_dir / "myrepo"
        wt.mkdir(parents=True)

        with (
            patch.object(mod, "workspace_dir", return_value=str(tmp_path)),
            patch.object(mod, "branch_prefix", return_value="ac"),
        ):
            result = mod.ws_ticket("123", "fix", ["myrepo"])
        assert result == 1

    def test_succeeds_when_at_least_one_worktree_created(self, tmp_path: Path) -> None:
        mod = load_script("ws_ticket")
        git_repo = tmp_path / "git-repo"
        (git_repo / ".git").mkdir(parents=True)
        skipped_repo = tmp_path / "not-a-repo"
        skipped_repo.mkdir()

        with (
            patch.object(mod, "workspace_dir", return_value=str(tmp_path)),
            patch.object(mod, "branch_prefix", return_value="ac"),
            patch.object(mod, "subprocess") as mock_sp,
        ):
            mock_sp.run.return_value = run_ok()
            result = mod.ws_ticket("123", "fix", ["not-a-repo", "git-repo"])
        assert result == 0

    def test_rollback_on_worktree_failure(self, tmp_path: Path) -> None:
        mod = load_script("ws_ticket")
        repo1 = tmp_path / "repo1"
        (repo1 / ".git").mkdir(parents=True)
        repo2 = tmp_path / "repo2"
        (repo2 / ".git").mkdir(parents=True)

        call_count = [0]

        def mock_run(cmd: list[str], **_kw: object) -> MagicMock:
            if "worktree" in cmd and "add" in cmd:
                call_count[0] += 1
                if call_count[0] == 2:
                    return run_ok(returncode=1, stderr="error")
                # First call: simulate success by creating the dir
                wt_path = cmd[-1]
                Path(wt_path).mkdir(parents=True, exist_ok=True)
                return run_ok()
            return run_ok()

        with (
            patch.object(mod, "workspace_dir", return_value=str(tmp_path)),
            patch.object(mod, "branch_prefix", return_value="ac"),
            patch.object(mod, "subprocess") as mock_sp,
        ):
            mock_sp.run.side_effect = mock_run
            result = mod.ws_ticket("123", "fix", ["repo1", "repo2"])

        assert result == 1

    def test_pull_failure_continues(self, tmp_path: Path) -> None:
        mod = load_script("ws_ticket")
        repo = tmp_path / "myrepo"
        (repo / ".git").mkdir(parents=True)

        def mock_run(cmd: list[str], **_kw: object) -> MagicMock:
            if "pull" in cmd:
                return run_ok(returncode=1)
            return run_ok()

        with (
            patch.object(mod, "workspace_dir", return_value=str(tmp_path)),
            patch.object(mod, "branch_prefix", return_value="ac"),
            patch.object(mod, "subprocess") as mock_sp,
        ):
            mock_sp.run.side_effect = mock_run
            result = mod.ws_ticket("123", "fix", ["myrepo"])

        assert result == 0
