"""Tests for resolve_repo_dir — worktree > main repo resolution."""

from pathlib import Path

import pytest
from lib.env import resolve_repo_dir


class TestResolveRepoDir:
    def test_returns_worktree_when_exists(
        self,
        workspace: Path,
        ticket_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        monkeypatch.chdir(ticket_dir / "my-project")

        result = resolve_repo_dir("my-project")
        assert result == str(ticket_dir / "my-project")

    def test_falls_back_to_main_repo_outside_ticket(
        self,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        monkeypatch.chdir(workspace)

        result = resolve_repo_dir("my-project")
        assert result == str(workspace / "my-project")

    def test_raises_when_repo_not_in_ticket_dir_strict(
        self,
        workspace: Path,
        ticket_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        monkeypatch.chdir(ticket_dir / "my-project")

        # my-translations doesn't exist in ticket dir → strict=True raises
        with pytest.raises(RuntimeError, match="my-translations worktree not found"):
            resolve_repo_dir("my-translations")

    def test_falls_back_when_strict_false(
        self,
        workspace: Path,
        ticket_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        monkeypatch.chdir(ticket_dir / "my-project")

        # Create main repo for translations
        trans = workspace / "my-translations"
        trans.mkdir()

        result = resolve_repo_dir("my-translations", strict=False)
        assert result == str(workspace / "my-translations")

    def test_uses_ticket_dir_env_var(
        self,
        workspace: Path,
        ticket_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        monkeypatch.setenv("TICKET_DIR", str(ticket_dir))
        monkeypatch.chdir(workspace)

        result = resolve_repo_dir("my-project")
        assert result == str(ticket_dir / "my-project")
