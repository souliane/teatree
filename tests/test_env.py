"""Tests for _env.py — environment detection and worktree context."""

import os
from pathlib import Path
from subprocess import CalledProcessError
from unittest.mock import patch

import pytest
from lib.env import (
    _rewrite_env_worktree,
    branch_prefix,
    detect_ticket_dir,
    find_free_ports,
    read_env_key,
    resolve_context,
    revalidate_ports,
    workspace_dir,
)


class TestWorkspaceDir:
    def test_returns_env_var_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_WORKSPACE_DIR", "/custom/workspace")
        assert workspace_dir() == "/custom/workspace"

    def test_falls_back_to_home_workspace(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("T3_WORKSPACE_DIR", raising=False)
        assert workspace_dir() == str(Path.home() / "workspace")


class TestBranchPrefix:
    def test_returns_env_var_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_BRANCH_PREFIX", "xx")
        assert branch_prefix() == "xx"

    def test_derives_from_git_user_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_BRANCH_PREFIX", raising=False)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = "Alice Cooper\n"
            mock_run.return_value.returncode = 0
            assert branch_prefix() == "ac"

    def test_single_name_returns_first_letter(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("T3_BRANCH_PREFIX", raising=False)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = "Zorro\n"
            mock_run.return_value.returncode = 0
            assert branch_prefix() == "z"

    def test_falls_back_to_wt_on_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_BRANCH_PREFIX", raising=False)
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = CalledProcessError(1, "git")
            assert branch_prefix() == "wt"

    def test_falls_back_to_wt_on_empty_name(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Branch 30->34: git user.name returns empty string."""
        monkeypatch.delenv("T3_BRANCH_PREFIX", raising=False)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = "\n"
            mock_run.return_value.returncode = 0
            assert branch_prefix() == "wt"


class TestDetectTicketDir:
    def test_returns_env_var_when_set(
        self,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        td = workspace / "ticket-1234"
        td.mkdir()
        monkeypatch.setenv("TICKET_DIR", str(td))
        assert detect_ticket_dir() == str(td)

    def test_returns_empty_when_env_dir_missing(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TICKET_DIR", "/nonexistent/path")
        assert detect_ticket_dir() == ""

    def test_detects_from_cwd_in_worktree(
        self,
        workspace: Path,
        ticket_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        wt_dir = ticket_dir / "my-project"
        monkeypatch.chdir(wt_dir)
        assert detect_ticket_dir() == str(ticket_dir)

    def test_returns_empty_outside_workspace(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(tmp_path / "workspace"))
        monkeypatch.chdir(tmp_path)
        assert detect_ticket_dir() == ""

    def test_returns_empty_for_main_repo(
        self,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Main repos have .git dirs — they are NOT ticket dirs."""
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        monkeypatch.chdir(workspace / "my-project")
        assert detect_ticket_dir() == ""


class TestResolveContext:
    def test_resolves_valid_worktree(
        self,
        workspace: Path,
        ticket_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        wt_dir = ticket_dir / "my-project"
        monkeypatch.chdir(wt_dir)

        ctx = resolve_context()

        assert ctx.wt_dir == str(wt_dir)
        assert ctx.ticket_dir == str(ticket_dir)
        assert ctx.ticket_number == "1234"
        assert ctx.main_repo == str(workspace / "my-project")
        assert ctx.repo_name == "my-project"

    def test_raises_outside_workspace(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(tmp_path / "workspace"))
        monkeypatch.chdir(tmp_path)
        with pytest.raises(RuntimeError, match="Not in a worktree"):
            resolve_context()

    def test_raises_when_main_repo_missing(
        self,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        td = workspace / "ticket-999-test"
        td.mkdir()
        wt = td / "nonexistent-repo"
        wt.mkdir()
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        monkeypatch.chdir(wt)
        with pytest.raises(RuntimeError, match="Main repo not found"):
            resolve_context()

    def test_raises_when_no_ticket_number(
        self,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        td = workspace / "no-number-here"
        td.mkdir()
        wt = td / "my-project"
        wt.mkdir()
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        monkeypatch.chdir(wt)
        with pytest.raises(RuntimeError, match="Could not extract ticket number"):
            resolve_context()

    def test_extracts_ticket_from_complex_name(
        self,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        td = workspace / "ac-my-project-5678-fix-bug"
        td.mkdir()
        wt = td / "my-project"
        wt.mkdir()
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        monkeypatch.chdir(wt)

        ctx = resolve_context()
        assert ctx.ticket_number == "5678"

    def test_resolves_from_ticket_dir_cwd(
        self,
        workspace: Path,
        ticket_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When CWD is the ticket dir itself, auto-detect repo."""
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        monkeypatch.chdir(ticket_dir)

        ctx = resolve_context()
        assert ctx.repo_name == "my-project"
        assert ctx.wt_dir == str(ticket_dir / "my-project")

    def test_resolves_from_ticket_dir_with_explicit_repo(
        self,
        workspace: Path,
        ticket_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When CWD is the ticket dir, explicit repo param works."""
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        monkeypatch.chdir(ticket_dir)

        ctx = resolve_context(repo="my-project")
        assert ctx.repo_name == "my-project"

    def test_resolves_from_nested_subdir(
        self,
        workspace: Path,
        ticket_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When CWD is nested under <ticket-dir>/<repo>/..., resolve repo correctly."""
        nested = ticket_dir / "my-project" / "app" / "settings"
        nested.mkdir(parents=True)
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        monkeypatch.chdir(nested)

        ctx = resolve_context()
        assert ctx.repo_name == "my-project"
        assert ctx.wt_dir == str(ticket_dir / "my-project")

    def test_raises_from_ticket_dir_no_repo(
        self,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When CWD is a ticket dir with no matching repos."""
        td = workspace / "ticket-999-empty"
        td.mkdir()
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        monkeypatch.chdir(td)

        with pytest.raises(RuntimeError, match="No repo found"):
            resolve_context()

    def test_raises_from_ticket_dir_child_not_a_repo(
        self,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Branch 92->91: children exist but none match a main repo (.git)."""
        td = workspace / "ticket-999-nomatch"
        td.mkdir()
        # Child dir exists but no corresponding main repo
        (td / "nonexistent-repo").mkdir()
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        monkeypatch.chdir(td)

        with pytest.raises(RuntimeError, match="No repo found"):
            resolve_context()

    def test_resolve_from_worktree_with_explicit_repo(
        self,
        workspace: Path,
        ticket_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Branch 102->106: repo_name provided explicitly when CWD is inside ticket dir/repo."""
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        wt_dir = ticket_dir / "my-project"
        monkeypatch.chdir(wt_dir)

        ctx = resolve_context(repo="my-project")
        assert ctx.repo_name == "my-project"
        assert ctx.wt_dir == str(wt_dir)


class TestFindFreePorts:
    def test_returns_defaults_when_no_envfiles(
        self,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        be, fe, pg, rd = find_free_ports()
        assert be == 8001
        assert fe == 4201
        assert pg == 5433
        assert rd == 6379  # Redis is shared, always 6379

    def test_skips_already_used_ports(
        self,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        # Create a ticket dir with an .env.worktree using port 8001
        td = workspace / "ticket-100"
        td.mkdir()
        (td / ".env.worktree").write_text(
            "DJANGO_RUNSERVER_PORT=8001\nFRONTEND_PORT=4201\n",
        )
        be, fe, _pg, _rd = find_free_ports()
        assert be == 8002
        assert fe == 4202

    def test_skips_multiple_used_ports(
        self,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        for i, ticket in enumerate(["ticket-100", "ticket-200"], start=1):
            td = workspace / ticket
            td.mkdir()
            (td / ".env.worktree").write_text(
                f"DJANGO_RUNSERVER_PORT={8000 + i}\nFRONTEND_PORT={4200 + i}\n",
            )
        be, fe, _pg, _rd = find_free_ports()
        assert be == 8003
        assert fe == 4203

    def test_ignores_symlinked_envfiles(
        self,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        td = workspace / "ticket-100"
        td.mkdir()
        real = td / ".env.worktree.real"
        real.write_text("DJANGO_RUNSERVER_PORT=8001\nFRONTEND_PORT=4201\n")
        (td / ".env.worktree").symlink_to(real)

        be, fe, _pg, _rd = find_free_ports()
        # Symlinked files are skipped → defaults returned
        assert be == 8001
        assert fe == 4201

    def test_excludes_specified_dir(
        self,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        td = workspace / "ticket-100"
        td.mkdir()
        (td / ".env.worktree").write_text(
            "DJANGO_RUNSERVER_PORT=8001\nFRONTEND_PORT=4201\n",
        )
        # Exclude this dir — so its ports aren't counted
        be, fe, _pg, _rd = find_free_ports(exclude_dir=str(td))
        assert be == 8001
        assert fe == 4201

    def test_ignores_invalid_port_values(
        self,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        td = workspace / "ticket-100"
        td.mkdir()
        (td / ".env.worktree").write_text(
            "BACKEND_PORT=notanumber\nFRONTEND_PORT=alsonotanumber\nPOSTGRES_PORT=badport\n",
        )
        be, fe, pg, _rd = find_free_ports()
        assert be == 8001
        assert fe == 4201
        assert pg == 5433

    def test_ignores_deep_directories(
        self,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        # Create a .env.worktree 4 levels deep — should be ignored (>2)
        deep = workspace / "a" / "b" / "c" / "d"
        deep.mkdir(parents=True)
        (deep / ".env.worktree").write_text("BACKEND_PORT=8001\n")

        be, _fe, _pg, _rd = find_free_ports()
        assert be == 8001  # Not used, starts at 8001

    def test_skips_already_used_postgres_ports(
        self,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        for i, ticket in enumerate(["ticket-100", "ticket-200"], start=1):
            td = workspace / ticket
            td.mkdir()
            (td / ".env.worktree").write_text(f"POSTGRES_PORT={5432 + i}\n")
        _be, _fe, pg, _rd = find_free_ports()
        assert pg == 5435

    def test_redis_always_shared_on_6379(
        self,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        for i, ticket in enumerate(["ticket-100", "ticket-200"], start=1):
            td = workspace / ticket
            td.mkdir()
            (td / ".env.worktree").write_text(f"REDIS_PORT={6379 + i}\n")
        _be, _fe, _pg, rd = find_free_ports()
        assert rd == 6379  # Redis is shared, always 6379

    def test_handles_oserror_reading_envfile(
        self,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        td = workspace / "ticket-100"
        td.mkdir()
        envwt = td / ".env.worktree"
        envwt.write_text("BACKEND_PORT=8001\n")
        # Make file unreadable
        envwt.chmod(0o000)
        try:
            be, _fe, _pg, _rd = find_free_ports()
            # Should handle OSError gracefully
            assert be == 8001
        finally:
            envwt.chmod(0o644)


class TestReadEnvKey:
    def test_reads_existing_key(self, tmp_path: Path) -> None:
        f = tmp_path / ".env"
        f.write_text("FOO=bar\nBAZ=qux\n")
        assert read_env_key(str(f), "FOO") == "bar"
        assert read_env_key(str(f), "BAZ") == "qux"

    def test_returns_empty_for_missing_key(self, tmp_path: Path) -> None:
        f = tmp_path / ".env"
        f.write_text("FOO=bar\n")
        assert read_env_key(str(f), "NOPE") == ""

    def test_returns_empty_for_missing_file(self) -> None:
        assert read_env_key("/nonexistent/path", "KEY") == ""

    def test_handles_values_with_equals(self, tmp_path: Path) -> None:
        f = tmp_path / ".env"
        f.write_text("URL=http://host:5432/db?opt=1\n")
        assert read_env_key(str(f), "URL") == "http://host:5432/db?opt=1"


class TestRewriteEnvWorktree:
    def test_replaces_matching_keys(self, tmp_path: Path) -> None:
        envfile = tmp_path / ".env.worktree"
        envfile.write_text("BACKEND_PORT=8001\nFRONTEND_PORT=4201\nOTHER=keep\n")
        _rewrite_env_worktree(str(envfile), {"BACKEND_PORT": 8005, "FRONTEND_PORT": 4205})
        content = envfile.read_text()
        assert "BACKEND_PORT=8005\n" in content
        assert "FRONTEND_PORT=4205\n" in content
        assert "OTHER=keep\n" in content

    def test_preserves_unmatched_lines(self, tmp_path: Path) -> None:
        envfile = tmp_path / ".env.worktree"
        envfile.write_text("A=1\nB=2\nC=3\n")
        _rewrite_env_worktree(str(envfile), {"B": 99})
        lines = envfile.read_text().splitlines()
        assert lines == ["A=1", "B=99", "C=3"]

    def test_handles_missing_file(self, tmp_path: Path) -> None:
        _rewrite_env_worktree(str(tmp_path / "nonexistent"), {"X": 1})  # no error


class TestRevalidatePorts:
    def test_returns_none_when_no_conflicts(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("BACKEND_PORT", "8001")
        monkeypatch.setenv("FRONTEND_PORT", "4201")
        monkeypatch.setenv("POSTGRES_PORT", "5433")
        # port_in_use is globally mocked to return False
        assert revalidate_ports() is None

    def test_reallocates_on_conflict(
        self,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        td = workspace / "ac-my-project-1234-test"
        td.mkdir()
        (td / "my-project").mkdir()
        envfile = td / ".env.worktree"
        envfile.write_text(
            "BACKEND_PORT=8001\nFRONTEND_PORT=4201\n"
            "POSTGRES_PORT=5433\n"
            "DJANGO_RUNSERVER_PORT=8001\n"
            "BACK_END_URL=http://localhost:8001\n"
            "FRONT_END_URL=http://localhost:4201\n"
            "DATABASE_URL=postgresql://u:p@localhost:5433/testdb\n"
        )
        monkeypatch.setenv("TICKET_DIR", str(td))
        monkeypatch.setenv("BACKEND_PORT", "8001")
        monkeypatch.setenv("DJANGO_RUNSERVER_PORT", "8001")
        monkeypatch.setenv("FRONTEND_PORT", "4201")
        monkeypatch.setenv("POSTGRES_PORT", "5433")
        monkeypatch.setenv("WT_DB_NAME", "testdb")
        # Simulate port 8001 in use
        monkeypatch.setattr(
            "lib.env.port_in_use",
            lambda port: port == 8001,
        )
        result = revalidate_ports()
        assert result is not None
        assert result["BACKEND_PORT"] != 8001
        # Env was updated
        assert os.environ["BACKEND_PORT"] == str(result["BACKEND_PORT"])

    def test_reallocates_without_db_name_and_envfile(
        self,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Covers falsy branches: no WT_DB_NAME and no .env.worktree file."""
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        td = workspace / "ac-my-project-5555-no-envfile"
        td.mkdir()
        (td / "my-project").mkdir()
        # No .env.worktree file, no WT_DB_NAME, no POSTGRES_DB
        monkeypatch.setenv("TICKET_DIR", str(td))
        monkeypatch.setenv("BACKEND_PORT", "8001")
        monkeypatch.setenv("FRONTEND_PORT", "4201")
        monkeypatch.setenv("POSTGRES_PORT", "5433")
        monkeypatch.delenv("WT_DB_NAME", raising=False)
        monkeypatch.delenv("POSTGRES_DB", raising=False)
        monkeypatch.setattr("lib.env.port_in_use", lambda port: port == 8001)
        result = revalidate_ports()
        assert result is not None
        assert "DATABASE_URL" not in result

    def test_returns_none_when_no_ticket_dir(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("BACKEND_PORT", "8001")
        monkeypatch.setenv("FRONTEND_PORT", "4201")
        monkeypatch.setenv("POSTGRES_PORT", "5433")
        monkeypatch.delenv("TICKET_DIR", raising=False)
        # Simulate conflict but no ticket dir
        monkeypatch.setattr("lib.env.port_in_use", lambda port: port == 8001)
        assert revalidate_ports() is None
