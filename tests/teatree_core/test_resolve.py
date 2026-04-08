"""Tests for teatree.core.resolve — worktree resolution from CWD."""

from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.models import Ticket, Worktree
from teatree.core.resolve import (
    WorktreeNotFoundError,
    _auto_register_from_git,
    _find_env_worktree,
    _match_worktree_by_path,
    _parse_env_file,
    _warn_cwd_mismatch,
    resolve_worktree,
)


class TestParseEnvFile:
    def test_basic(self, tmp_path: Path) -> None:
        envfile = tmp_path / ".env"
        envfile.write_text("KEY=value\nOTHER=123\n", encoding="utf-8")

        result = _parse_env_file(envfile)

        assert result == {"KEY": "value", "OTHER": "123"}

    def test_skips_comments_and_empty_lines(self, tmp_path: Path) -> None:
        envfile = tmp_path / ".env"
        envfile.write_text("# comment\n\nKEY=value\n  # indented comment\n\n", encoding="utf-8")

        result = _parse_env_file(envfile)

        assert result == {"KEY": "value"}

    def test_strips_whitespace(self, tmp_path: Path) -> None:
        envfile = tmp_path / ".env"
        envfile.write_text("  KEY  =  value  \n", encoding="utf-8")

        result = _parse_env_file(envfile)

        assert result == {"KEY": "value"}

    def test_skips_lines_without_equals(self, tmp_path: Path) -> None:
        envfile = tmp_path / ".env"
        envfile.write_text("NOEQUALS\nKEY=value\n", encoding="utf-8")

        result = _parse_env_file(envfile)

        assert result == {"KEY": "value"}

    def test_value_with_equals(self, tmp_path: Path) -> None:
        """Values containing '=' should be preserved (partition splits on first '=')."""
        envfile = tmp_path / ".env"
        envfile.write_text("URL=http://host?a=b\n", encoding="utf-8")

        result = _parse_env_file(envfile)

        assert result == {"URL": "http://host?a=b"}


class TestFindEnvWorktree:
    def test_found_in_cwd(self, tmp_path: Path) -> None:
        envfile = tmp_path / ".env.worktree"
        envfile.write_text("TICKET_DIR=/some/path\n", encoding="utf-8")

        result = _find_env_worktree(str(tmp_path))

        assert result == envfile

    def test_found_in_parent(self, tmp_path: Path) -> None:
        envfile = tmp_path / ".env.worktree"
        envfile.write_text("TICKET_DIR=/some/path\n", encoding="utf-8")
        child = tmp_path / "sub" / "deep"
        child.mkdir(parents=True)

        result = _find_env_worktree(str(child))

        assert result == envfile

    def test_not_found(self, tmp_path: Path) -> None:
        child = tmp_path / "a" / "b"
        child.mkdir(parents=True)

        result = _find_env_worktree(str(child))

        assert result is None


class TestMatchWorktreeByPath(TestCase):
    def test_exact_match(self) -> None:
        ticket = Ticket.objects.create()
        wt = Worktree.objects.create(
            ticket=ticket,
            repo_path="backend",
            branch="feature",
            extra={"worktree_path": "/workspace/ac-backend-42/backend"},
        )

        result = _match_worktree_by_path("/workspace/ac-backend-42/backend")

        assert result is not None
        assert result.pk == wt.pk

    def test_prefix_match(self) -> None:
        ticket = Ticket.objects.create()
        wt = Worktree.objects.create(
            ticket=ticket,
            repo_path="backend",
            branch="feature",
            extra={"worktree_path": "/workspace/ac-backend-42"},
        )

        result = _match_worktree_by_path("/workspace/ac-backend-42/backend/src")

        assert result is not None
        assert result.pk == wt.pk

    def test_no_match(self) -> None:
        ticket = Ticket.objects.create()
        Worktree.objects.create(
            ticket=ticket,
            repo_path="backend",
            branch="feature",
            extra={"worktree_path": "/workspace/ac-backend-42"},
        )

        result = _match_worktree_by_path("/totally/different/path")

        assert result is None

    def test_skips_empty_extra(self) -> None:
        ticket = Ticket.objects.create()
        Worktree.objects.create(ticket=ticket, repo_path="backend", branch="feature", extra={})

        result = _match_worktree_by_path("/some/path")

        assert result is None


class TestResolveWorktree(TestCase):
    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._monkeypatch = monkeypatch
        self._tmp_path = tmp_path

    def test_from_env_worktree_file(self) -> None:
        ticket = Ticket.objects.create()
        wt = Worktree.objects.create(
            ticket=ticket,
            repo_path="backend",
            branch="feature",
            extra={"worktree_path": str(self._tmp_path / "ticket-dir")},
        )

        envfile = self._tmp_path / ".env.worktree"
        envfile.write_text(f"TICKET_DIR={self._tmp_path / 'ticket-dir'}\n", encoding="utf-8")
        self._monkeypatch.setenv("T3_ORIG_CWD", str(self._tmp_path))

        result = resolve_worktree()

        assert result.pk == wt.pk

    def test_from_cwd_path(self) -> None:
        ticket = Ticket.objects.create()
        wt_path = str(self._tmp_path / "workspace" / "ac-backend-42")
        wt = Worktree.objects.create(
            ticket=ticket,
            repo_path="backend",
            branch="feature",
            extra={"worktree_path": wt_path},
        )
        self._monkeypatch.setenv("T3_ORIG_CWD", wt_path)

        result = resolve_worktree()

        assert result.pk == wt.pk

    def test_auto_registers_git_worktree(self) -> None:
        """Step 3: auto-register when .git is a file (git worktree marker)."""
        wt_dir = self._tmp_path / "my-repo"
        wt_dir.mkdir()
        (wt_dir / ".git").write_text("gitdir: /some/main/.git/worktrees/my-repo\n")
        self._monkeypatch.setenv("T3_ORIG_CWD", str(wt_dir))

        with patch("teatree.core.resolve.git.current_branch", return_value="feat/branch"):
            result = resolve_worktree()

        assert result.branch == "feat/branch"
        assert result.repo_path == "my-repo"
        assert result.extra["worktree_path"] == str(wt_dir)

    def test_raises_when_nothing_found(self) -> None:
        self._monkeypatch.setenv("T3_ORIG_CWD", str(self._tmp_path))

        with pytest.raises(WorktreeNotFoundError, match="Cannot auto-detect worktree"):
            resolve_worktree()

    def test_t3_orig_cwd_takes_precedence(self) -> None:
        """T3_ORIG_CWD should be used instead of actual CWD."""
        ticket = Ticket.objects.create()
        wt_path = str(self._tmp_path / "correct")
        Worktree.objects.create(
            ticket=ticket,
            repo_path="backend",
            branch="feature",
            extra={"worktree_path": wt_path},
        )
        self._monkeypatch.setenv("T3_ORIG_CWD", wt_path)

        result = resolve_worktree()

        assert result.extra["worktree_path"] == wt_path

    def test_env_file_without_ticket_dir(self) -> None:
        """When .env.worktree exists but has no TICKET_DIR, fall through to CWD match."""
        envfile = self._tmp_path / ".env.worktree"
        envfile.write_text("SOME_OTHER_KEY=value\n", encoding="utf-8")
        self._monkeypatch.setenv("T3_ORIG_CWD", str(self._tmp_path))

        with pytest.raises(WorktreeNotFoundError):
            resolve_worktree()


class TestWarnCwdMismatch(TestCase):
    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, caplog: pytest.LogCaptureFixture) -> None:
        self._caplog = caplog

    def test_no_warning_when_cwd_inside_worktree(self) -> None:
        """No warning when CWD is inside the worktree path."""
        ticket = Ticket.objects.create()
        wt = Worktree.objects.create(
            ticket=ticket,
            repo_path="backend",
            branch="feature",
            extra={"worktree_path": "/workspace/ticket-42/backend"},
        )

        with self._caplog.at_level("WARNING", logger="teatree.core.resolve"):
            _warn_cwd_mismatch(wt, "/workspace/ticket-42/backend/src")

        assert not self._caplog.records

    def test_no_warning_when_cwd_matches_exactly(self) -> None:
        """No warning when CWD matches worktree path exactly."""
        ticket = Ticket.objects.create()
        wt = Worktree.objects.create(
            ticket=ticket,
            repo_path="backend",
            branch="feature",
            extra={"worktree_path": "/workspace/ticket-42/backend"},
        )

        with self._caplog.at_level("WARNING", logger="teatree.core.resolve"):
            _warn_cwd_mismatch(wt, "/workspace/ticket-42/backend")

        assert not self._caplog.records

    def test_warning_when_cwd_outside_worktree(self) -> None:
        """Warning when CWD doesn't match the resolved worktree path."""
        ticket = Ticket.objects.create()
        wt = Worktree.objects.create(
            ticket=ticket,
            repo_path="backend",
            branch="feature",
            extra={"worktree_path": "/workspace/ticket-42/backend"},
        )

        with self._caplog.at_level("WARNING", logger="teatree.core.resolve"):
            _warn_cwd_mismatch(wt, "/totally/different/path")

        assert len(self._caplog.records) == 1
        assert "does not match CWD" in self._caplog.records[0].message

    def test_no_warning_when_worktree_inside_cwd(self) -> None:
        """No warning when the worktree path is a subdirectory of CWD (ticket dir)."""
        ticket = Ticket.objects.create()
        wt = Worktree.objects.create(
            ticket=ticket,
            repo_path="backend",
            branch="feature",
            extra={"worktree_path": "/workspace/ticket-42/backend"},
        )

        with self._caplog.at_level("WARNING", logger="teatree.core.resolve"):
            _warn_cwd_mismatch(wt, "/workspace/ticket-42")

        assert not self._caplog.records

    def test_no_warning_when_no_worktree_path(self) -> None:
        """No warning when worktree has no stored path."""
        ticket = Ticket.objects.create()
        wt = Worktree.objects.create(
            ticket=ticket,
            repo_path="backend",
            branch="feature",
            extra={},
        )

        with self._caplog.at_level("WARNING", logger="teatree.core.resolve"):
            _warn_cwd_mismatch(wt, "/some/path")

        assert not self._caplog.records


class TestAutoRegisterFromGit:
    def test_returns_none_when_git_fails(self, tmp_path: Path) -> None:
        """Git command failure should return None, not raise."""
        wt_dir = tmp_path / "my-repo"
        wt_dir.mkdir()
        (wt_dir / ".git").write_text("gitdir: /some/.git/worktrees/my-repo\n")

        with patch("teatree.core.resolve.git.current_branch", return_value=""):
            assert _auto_register_from_git(str(wt_dir)) is None

    def test_returns_none_when_branch_empty(self, tmp_path: Path) -> None:
        """Detached HEAD (empty branch) should return None."""
        wt_dir = tmp_path / "my-repo"
        wt_dir.mkdir()
        (wt_dir / ".git").write_text("gitdir: /some/.git/worktrees/my-repo\n")

        with patch("teatree.core.resolve.git.current_branch", return_value=""):
            assert _auto_register_from_git(str(wt_dir)) is None
