"""Tests for teatree/scripts/git_clean_them_all.py — ticket-atomic cleanup."""

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from conftest import load_script, run_ok

# ---------------------------------------------------------------------------
# Helpers for mock_run dispatch
# ---------------------------------------------------------------------------


def _dispatch_toplevel(
    cmd: list[str],
    kw: dict[str, object],
    repo: str,
) -> MagicMock | None:
    if "--show-toplevel" not in cmd:
        return None
    cwd = kw.get("cwd", "")
    return run_ok(stdout=repo) if cwd == repo else run_ok(returncode=1)


def _dispatch_worktree(cmd: list[str], porcelain: str) -> MagicMock | None:
    if "worktree" not in cmd:
        return None
    if "list" in cmd:
        return run_ok(stdout=porcelain)
    if "prune" in cmd:
        return run_ok()
    if "remove" in cmd:
        return run_ok()
    return None


def _dispatch_dirty(cmd: list[str], *, dirty: bool = False) -> MagicMock | None:
    if "diff" in cmd and "--quiet" in cmd:
        return run_ok(returncode=1 if dirty else 0)
    if "ls-files" in cmd:
        return run_ok(stdout="")
    return None


def _dispatch_branch(
    cmd: list[str],
    *,
    merged: str = "",
    gone: str = "",
    all_branches: str = "",
) -> MagicMock | None:
    if "--merged" in cmd:
        return run_ok(stdout=merged)
    if "--format" in cmd:
        fmt = cmd[cmd.index("--format") + 1]
        if "%(upstream:track)" in fmt:
            return run_ok(stdout=gone)
        if "%(refname:short)" in fmt:
            return run_ok(stdout=all_branches)
    if "-D" in cmd:
        return run_ok()
    return None


def _dispatch_infra(cmd: list[str]) -> MagicMock | None:
    """Handle docker/psql/dropdb commands as no-ops."""
    if cmd and cmd[0] in {"docker", "psql", "dropdb"}:
        return run_ok(stdout="")
    return None


@dataclass
class MockOpts:
    """Options for _dispatch_common — keeps the arg count under the ruff limit."""

    repo: str
    porcelain: str
    merged: str = ""
    gone: str = ""
    all_branches: str = ""
    dirty: bool = False


def _dispatch_common(
    cmd: list[str],
    kw: dict[str, object],
    opts: MockOpts,
) -> MagicMock | None:
    """Route git commands for single-repo test scenarios."""
    for dispatch in [
        lambda: _dispatch_toplevel(cmd, kw, opts.repo),
        lambda: _dispatch_worktree(cmd, opts.porcelain),
        lambda: run_ok() if "fetch" in cmd else None,
        lambda: _dispatch_branch(cmd, merged=opts.merged, gone=opts.gone, all_branches=opts.all_branches),
        lambda: _dispatch_dirty(cmd, dirty=opts.dirty),
        lambda: _dispatch_infra(cmd),
    ]:
        result = dispatch()
        if result is not None:
            return result
    return None


# ---------------------------------------------------------------------------
# TestGitCleanThemAll
# ---------------------------------------------------------------------------


class TestGitCleanThemAll:
    def test_no_repos_found(self, tmp_path: Path) -> None:
        mod = load_script("git_clean_them_all")
        ws = tmp_path / "workspace"
        ws.mkdir()
        with patch.object(mod, "workspace_dir", return_value=str(ws)):
            result = mod.git_clean_them_all()
        assert result == 1

    def test_no_worktrees_to_clean(self, tmp_path: Path) -> None:
        mod = load_script("git_clean_them_all")
        ws = tmp_path / "workspace"
        repo = ws / "myrepo"
        repo.mkdir(parents=True)
        (repo / ".git").mkdir()

        # Include a worktree that's NOT inside a ticket dir (e.g. under the main repo)
        # to exercise the continue-when-no-ticket-dir branch in _inventory
        extra_wt = f"worktree {repo / 'sub'}\nbranch refs/heads/feat\n\n"
        porcelain = f"worktree {repo}\nbranch refs/heads/master\n\n{extra_wt}"

        def mock_run(cmd: list[str], **kw: object) -> MagicMock:
            return _dispatch_common(cmd, kw, MockOpts(repo=str(repo), porcelain=porcelain)) or run_ok()

        with (
            patch.object(mod, "subprocess") as mock_sp,
            patch.object(mod, "default_branch", return_value="master"),
            patch.object(mod, "workspace_dir", return_value=str(ws)),
        ):
            mock_sp.run.side_effect = mock_run
            result = mod.git_clean_them_all()
        assert result == 0

    def test_partial_cleanup_when_one_repo_not_merged(
        self,
        tmp_path: Path,
    ) -> None:
        """Two repos in a ticket: one merged, one not → remove the merged one, keep the other."""
        mod = load_script("git_clean_them_all")
        ws = tmp_path / "workspace"

        repo_be = ws / "my-frontend"
        repo_be.mkdir(parents=True)
        (repo_be / ".git").mkdir()

        repo_fe = ws / "my-project"
        repo_fe.mkdir(parents=True)
        (repo_fe / ".git").mkdir()

        ticket = ws / "ac-my-project-1234-fix"
        wt_be = ticket / "my-project"
        wt_be.mkdir(parents=True)
        wt_fe = ticket / "my-frontend"
        wt_fe.mkdir(parents=True)

        repos = {
            str(repo_be): f"worktree {repo_be}\nbranch refs/heads/master\n\n"
            f"worktree {wt_fe}\nbranch refs/heads/feat\n\n",
            str(repo_fe): f"worktree {repo_fe}\nbranch refs/heads/master\n\n"
            f"worktree {wt_be}\nbranch refs/heads/feat\n\n",
        }
        removed: list[str] = []

        def mock_run(cmd: list[str], **kw: object) -> MagicMock:
            cwd = str(kw.get("cwd", ""))
            if "--show-toplevel" in cmd:
                return run_ok(stdout=cwd) if cwd in repos else run_ok(returncode=1)
            if "worktree" in cmd and "list" in cmd:
                return run_ok(stdout=repos.get(cwd, ""))
            wt = _dispatch_worktree(cmd, "")
            if wt is not None:
                if "remove" in cmd:
                    removed.append(cmd[4])
                return wt
            if "fetch" in cmd:
                return run_ok(stderr="")
            # my-project (repo_fe) is merged, my-frontend (repo_be) is NOT merged
            if "--merged" in cmd:
                return run_ok(stdout="  feat\n") if cwd == str(repo_fe) else run_ok(stdout="")
            return (
                _dispatch_branch(cmd, gone="feat\n", all_branches="feat\n")
                or _dispatch_dirty(cmd)
                or _dispatch_infra(cmd)
                or run_ok()
            )

        with (
            patch.object(mod, "subprocess") as mock_sp,
            patch.object(mod, "default_branch", return_value="master"),
            patch.object(mod, "workspace_dir", return_value=str(ws)),
        ):
            mock_sp.run.side_effect = mock_run
            result = mod.git_clean_them_all()

        assert result == 0
        # The merged worktree should be removed, the non-merged one kept
        assert len(removed) == 1
        assert str(wt_be) in removed

    def test_skips_ticket_when_dirty(self, tmp_path: Path) -> None:
        mod = load_script("git_clean_them_all")
        ws = tmp_path / "workspace"
        repo = ws / "repo"
        repo.mkdir(parents=True)
        (repo / ".git").mkdir()
        ticket = ws / "ac-repo-99-test"
        wt = ticket / "repo"
        wt.mkdir(parents=True)

        porcelain = f"worktree {repo}\nbranch refs/heads/master\n\nworktree {wt}\nbranch refs/heads/feat\n\n"

        def mock_run(cmd: list[str], **kw: object) -> MagicMock:
            return (
                _dispatch_common(
                    cmd,
                    kw,
                    MockOpts(repo=str(repo), porcelain=porcelain, merged="  feat\n", all_branches="feat\n", dirty=True),
                )
                or run_ok()
            )

        with (
            patch.object(mod, "subprocess") as mock_sp,
            patch.object(mod, "default_branch", return_value="master"),
            patch.object(mod, "workspace_dir", return_value=str(ws)),
        ):
            mock_sp.run.side_effect = mock_run
            result = mod.git_clean_them_all()
        assert result == 0

    def test_removes_fully_merged_ticket(self, tmp_path: Path) -> None:
        """Single worktree, merged and clean → full cleanup."""
        mod = load_script("git_clean_them_all")
        ws = tmp_path / "workspace"
        repo = ws / "repo"
        repo.mkdir(parents=True)
        (repo / ".git").mkdir()
        ticket = ws / "ac-repo-42-feature"
        wt = ticket / "repo"
        wt.mkdir(parents=True)
        (ticket / ".env.worktree").write_text("WT_DB_NAME=wt_42\nCOMPOSE_PROJECT_NAME=repo-wt42\n")

        porcelain = f"worktree {repo}\nbranch refs/heads/master\n\nworktree {wt}\nbranch refs/heads/feat\n\n"
        removed: list[str] = []

        def mock_run(cmd: list[str], **kw: object) -> MagicMock:
            if "worktree" in cmd and "remove" in cmd:
                removed.append(str(cmd))
            opts = MockOpts(repo=str(repo), porcelain=porcelain, merged="  feat\n", all_branches="feat\n")
            return _dispatch_common(cmd, kw, opts) or run_ok()

        with (
            patch.object(mod, "subprocess") as mock_sp,
            patch.object(mod, "default_branch", return_value="master"),
            patch.object(mod, "workspace_dir", return_value=str(ws)),
        ):
            mock_sp.run.side_effect = mock_run
            result = mod.git_clean_them_all()

        assert result == 0
        assert len(removed) == 1
        assert not (ticket / ".env.worktree").exists()

    def test_detects_gone_branch(self, tmp_path: Path) -> None:
        mod = load_script("git_clean_them_all")
        ws = tmp_path / "workspace"
        repo = ws / "repo"
        repo.mkdir(parents=True)
        (repo / ".git").mkdir()
        ticket = ws / "ac-repo-55-gone"
        wt = ticket / "repo"
        wt.mkdir(parents=True)

        porcelain = f"worktree {repo}\nbranch refs/heads/master\n\nworktree {wt}\nbranch refs/heads/feat\n\n"
        removed: list[str] = []

        def mock_run(cmd: list[str], **kw: object) -> MagicMock:
            if "worktree" in cmd and "remove" in cmd:
                removed.append(str(cmd))
            return (
                _dispatch_common(
                    cmd,
                    kw,
                    MockOpts(repo=str(repo), porcelain=porcelain, gone="feat [gone]\n", all_branches="feat\n"),
                )
                or run_ok()
            )

        with (
            patch.object(mod, "subprocess") as mock_sp,
            patch.object(mod, "default_branch", return_value="master"),
            patch.object(mod, "workspace_dir", return_value=str(ws)),
        ):
            mock_sp.run.side_effect = mock_run
            result = mod.git_clean_them_all()

        assert result == 0
        assert len(removed) == 1

    def test_cleans_orphaned_branches(self, tmp_path: Path) -> None:
        """A merged branch with no worktree should be deleted."""
        mod = load_script("git_clean_them_all")
        ws = tmp_path / "workspace"
        repo = ws / "repo"
        repo.mkdir(parents=True)
        (repo / ".git").mkdir()

        deleted_branches: list[str] = []

        def mock_run(cmd: list[str], **kw: object) -> MagicMock:
            result = _dispatch_common(
                cmd,
                kw,
                MockOpts(
                    repo=str(repo),
                    porcelain=f"worktree {repo}\nbranch refs/heads/master\n\n",
                    merged="  old-merged-branch\n",
                    gone="old-merged-branch [gone]\n",
                    all_branches="old-merged-branch\n",
                ),
            )
            if result is not None:
                if "-D" in cmd and len(cmd) > cmd.index("-D") + 1:
                    deleted_branches.append(cmd[cmd.index("-D") + 1])
                return result
            return run_ok()

        with (
            patch.object(mod, "subprocess") as mock_sp,
            patch.object(mod, "default_branch", return_value="master"),
            patch.object(mod, "workspace_dir", return_value=str(ws)),
        ):
            mock_sp.run.side_effect = mock_run
            result = mod.git_clean_them_all()

        assert result == 0
        assert "old-merged-branch" in deleted_branches

    def test_skips_non_merged_orphan_branches(self, tmp_path: Path) -> None:
        """A non-merged/non-gone branch with no worktree → left alone."""
        mod = load_script("git_clean_them_all")
        ws = tmp_path / "workspace"
        repo = ws / "repo"
        repo.mkdir(parents=True)
        (repo / ".git").mkdir()

        deleted_branches: list[str] = []

        def mock_run(cmd: list[str], **kw: object) -> MagicMock:
            result = _dispatch_common(
                cmd,
                kw,
                MockOpts(
                    repo=str(repo),
                    porcelain=f"worktree {repo}\nbranch refs/heads/master\n\n",
                    merged="",  # NOT merged
                    gone="",  # NOT gone
                    all_branches="unmerged-branch\n",
                ),
            )
            if result is not None:
                if "-D" in cmd and len(cmd) > cmd.index("-D") + 1:
                    deleted_branches.append(cmd[cmd.index("-D") + 1])
                return result
            return run_ok()

        with (
            patch.object(mod, "subprocess") as mock_sp,
            patch.object(mod, "default_branch", return_value="master"),
            patch.object(mod, "workspace_dir", return_value=str(ws)),
        ):
            mock_sp.run.side_effect = mock_run
            result = mod.git_clean_them_all()

        assert result == 0
        assert deleted_branches == []

    def test_skips_protected_branch_worktrees(self, tmp_path: Path) -> None:
        """A worktree on main/master/development is never considered removable."""
        mod = load_script("git_clean_them_all")
        ws = tmp_path / "workspace"
        repo = ws / "repo"
        repo.mkdir(parents=True)
        (repo / ".git").mkdir()
        ticket = ws / "ac-repo-77-ruff"
        wt = ticket / "repo"
        wt.mkdir(parents=True)

        # Worktree is on the "development" branch (a protected name)
        porcelain = f"worktree {repo}\nbranch refs/heads/master\n\nworktree {wt}\nbranch refs/heads/development\n\n"
        removed: list[str] = []

        def mock_run(cmd: list[str], **kw: object) -> MagicMock:
            if "worktree" in cmd and "remove" in cmd:
                removed.append(str(cmd))
            return (
                _dispatch_common(
                    cmd,
                    kw,
                    MockOpts(
                        repo=str(repo),
                        porcelain=porcelain,
                        merged="  development\n",
                        all_branches="development\n",
                    ),
                )
                or run_ok()
            )

        with (
            patch.object(mod, "subprocess") as mock_sp,
            patch.object(mod, "default_branch", return_value="master"),
            patch.object(mod, "workspace_dir", return_value=str(ws)),
        ):
            mock_sp.run.side_effect = mock_run
            result = mod.git_clean_them_all()

        assert result == 0
        assert removed == []

    def test_skips_protected_orphan_branches(self, tmp_path: Path) -> None:
        """Protected branch names are never deleted as orphans."""
        mod = load_script("git_clean_them_all")
        ws = tmp_path / "workspace"
        repo = ws / "repo"
        repo.mkdir(parents=True)
        (repo / ".git").mkdir()

        deleted_branches: list[str] = []

        def mock_run(cmd: list[str], **kw: object) -> MagicMock:
            result = _dispatch_common(
                cmd,
                kw,
                MockOpts(
                    repo=str(repo),
                    porcelain=f"worktree {repo}\nbranch refs/heads/master\n\n",
                    merged="  main\n  development\n",
                    all_branches="main\ndevelopment\n",
                ),
            )
            if result is not None:
                if "-D" in cmd and len(cmd) > cmd.index("-D") + 1:
                    deleted_branches.append(cmd[cmd.index("-D") + 1])
                return result
            return run_ok()

        with (
            patch.object(mod, "subprocess") as mock_sp,
            patch.object(mod, "default_branch", return_value="master"),
            patch.object(mod, "workspace_dir", return_value=str(ws)),
        ):
            mock_sp.run.side_effect = mock_run
            result = mod.git_clean_them_all()

        assert result == 0
        assert deleted_branches == []

    def test_skips_repo_with_bad_default_branch(self, tmp_path: Path) -> None:
        mod = load_script("git_clean_them_all")
        ws = tmp_path / "workspace"
        repo = ws / "repo"
        repo.mkdir(parents=True)
        (repo / ".git").mkdir()

        def mock_run(cmd: list[str], **_kw: object) -> MagicMock:
            if "fetch" in cmd:
                return run_ok()
            if "worktree" in cmd and "prune" in cmd:
                return run_ok()
            return run_ok()

        with (
            patch.object(mod, "subprocess") as mock_sp,
            patch.object(mod, "default_branch", side_effect=RuntimeError("no default")),
            patch.object(mod, "workspace_dir", return_value=str(ws)),
        ):
            mock_sp.run.side_effect = mock_run
            result = mod.git_clean_them_all()
        assert result == 0


class TestParseWorktrees:
    def test_skips_unexpected_lines(self) -> None:
        mod = load_script("git_clean_them_all")
        output = "worktree /path\nHEAD abc123\nbranch refs/heads/feat\n\n"
        result = mod._parse_worktrees(output)
        assert result == [("/path", "feat")]

    def test_skips_when_path_missing(self) -> None:
        mod = load_script("git_clean_them_all")
        output = "branch refs/heads/feat\n\n"
        result = mod._parse_worktrees(output)
        assert result == []

    def test_trailing_entry_without_blank(self) -> None:
        mod = load_script("git_clean_them_all")
        output = "worktree /a\nbranch refs/heads/master\n\nworktree /b\nbranch refs/heads/feat"
        result = mod._parse_worktrees(output)
        assert result == [("/a", "master"), ("/b", "feat")]


class TestDirtyReason:
    def test_staged_changes(self) -> None:
        mod = load_script("git_clean_them_all")
        with patch.object(mod, "subprocess") as mock_sp:
            mock_sp.run.side_effect = [
                run_ok(returncode=0),  # git diff --quiet → clean
                run_ok(returncode=1),  # git diff --cached --quiet → dirty
            ]
            assert mod._dirty_reason("/fake") == "uncommitted changes"

    def test_ignores_shared_dirs_and_uv_lock(self) -> None:
        mod = load_script("git_clean_them_all")
        with patch.object(mod, "subprocess") as mock_sp:
            mock_sp.run.side_effect = [
                run_ok(returncode=0),  # git diff --quiet → clean
                run_ok(returncode=0),  # git diff --cached --quiet → clean
                run_ok(stdout=".data/dump.pgsql\nuv.lock\n"),
            ]
            assert mod._dirty_reason("/fake") == ""

    def test_ignores_t3_clean_ignore_patterns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = load_script("git_clean_them_all")
        monkeypatch.setenv("T3_CLEAN_IGNORE", "staticfiles/,e2e/,max_migration.txt,.env")
        # Force re-build of ignore sets by providing empty cached sets
        monkeypatch.setattr(mod, "_IGNORED_UNTRACKED_DIRS", frozenset())
        monkeypatch.setattr(mod, "_IGNORED_UNTRACKED_FILES", frozenset())
        with patch.object(mod, "subprocess") as mock_sp:
            mock_sp.run.side_effect = [
                run_ok(returncode=0),
                run_ok(returncode=0),
                run_ok(
                    stdout="staticfiles/i18n/de-AT.json\n"
                    "advisormodule/migrations/max_migration.txt\n"
                    "e2e/node_modules/.package-lock.json\n"
                    ".env\n"
                ),
            ]
            assert mod._dirty_reason("/fake") == ""

    def test_reports_significant_untracked(self) -> None:
        mod = load_script("git_clean_them_all")
        with patch.object(mod, "subprocess") as mock_sp:
            mock_sp.run.side_effect = [
                run_ok(returncode=0),  # git diff --quiet → clean
                run_ok(returncode=0),  # git diff --cached --quiet → clean
                run_ok(stdout=".data/dump.pgsql\nreal_file.py\n"),  # untracked
            ]
            assert mod._dirty_reason("/fake") == "untracked files"


class TestTicketDirForWorktree:
    def test_returns_parent_under_workspace(self, tmp_path: Path) -> None:
        mod = load_script("git_clean_them_all")
        ws = str(tmp_path / "ws")
        wt = f"{ws}/ticket/repo"
        assert mod._ticket_dir_for_worktree(ws, wt) == f"{ws}/ticket"

    def test_returns_none_outside_workspace(self) -> None:
        mod = load_script("git_clean_them_all")
        assert mod._ticket_dir_for_worktree("/ws", "/other/ticket/repo") is None

    def test_returns_none_for_main_repo(self, tmp_path: Path) -> None:
        mod = load_script("git_clean_them_all")
        ws = tmp_path / "ws"
        main_repo = ws / "repo"
        main_repo.mkdir(parents=True)
        (main_repo / ".git").mkdir()
        wt_path = str(main_repo / "sub")
        assert mod._ticket_dir_for_worktree(str(ws), wt_path) is None


class TestExtractTicketNumber:
    def test_extracts_digits(self) -> None:
        mod = load_script("git_clean_them_all")
        assert mod._extract_ticket_number("ac-my-project-1234-fix") == "1234"

    def test_no_digits(self) -> None:
        mod = load_script("git_clean_them_all")
        assert mod._extract_ticket_number("no-digits-here") == ""


class TestReadTicketEnv:
    def test_reads_values(self, tmp_path: Path) -> None:
        mod = load_script("git_clean_them_all")
        (tmp_path / ".env.worktree").write_text(
            "WT_DB_NAME=wt_42\nCOMPOSE_PROJECT_NAME=repo-wt42\nOTHER=x\n",
        )
        env = mod._read_ticket_env(str(tmp_path))
        assert env.db_name == "wt_42"
        assert env.compose_project_name == "repo-wt42"

    def test_missing_file(self, tmp_path: Path) -> None:
        mod = load_script("git_clean_them_all")
        env = mod._read_ticket_env(str(tmp_path))
        assert env.db_name == ""
        assert env.compose_project_name == ""


class TestRemoveTicketDir:
    def test_removes_empty_dir(self, tmp_path: Path) -> None:
        mod = load_script("git_clean_them_all")
        td = tmp_path / "ticket"
        td.mkdir()
        mod._remove_ticket_dir(str(td))
        assert not td.exists()

    def test_removes_generated_files(self, tmp_path: Path) -> None:
        mod = load_script("git_clean_them_all")
        td = tmp_path / "ticket"
        td.mkdir()
        (td / ".env.worktree").write_text("x")
        (td / "frontend.log").write_text("x")
        direnv = td / ".direnv"
        direnv.mkdir()
        (direnv / "allow").write_text("x")

        mod._remove_ticket_dir(str(td))
        assert not td.exists()

    def test_warns_on_nonempty(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        mod = load_script("git_clean_them_all")
        td = tmp_path / "ticket"
        td.mkdir()
        (td / "mystery_file").write_text("x")

        mod._remove_ticket_dir(str(td))
        assert td.exists()
        out = capsys.readouterr().out
        assert "WARNING" in out
        assert "mystery_file" in out

    def test_nonexistent_dir(self) -> None:
        mod = load_script("git_clean_them_all")
        mod._remove_ticket_dir("/nonexistent/path")  # should return silently


class TestFetchAndReport:
    def test_prints_relevant_stderr_lines(self, capsys: pytest.CaptureFixture[str]) -> None:
        mod = load_script("git_clean_them_all")
        with patch.object(mod, "subprocess") as mock_sp:
            mock_sp.run.return_value = run_ok(
                stderr="From origin\n  - [deleted] old-branch\nnoise\n",
            )
            mod._fetch_and_report("/fake/repo")
        out = capsys.readouterr().out
        assert "From origin" in out
        assert "[deleted]" in out
        assert "noise" not in out


class TestDockerRmByLabel:
    def test_removes_containers_volumes_networks(self) -> None:
        mod = load_script("git_clean_them_all")
        calls: list[list[str]] = []

        def mock_run(cmd: list[str], **_kw: object) -> MagicMock:
            calls.append(cmd)
            if "ps" in cmd:
                return run_ok(stdout="abc123\n")
            if "volume" in cmd and "ls" in cmd:
                return run_ok(stdout="vol1\n")
            if "network" in cmd and "ls" in cmd:
                return run_ok(stdout="net1\n")
            return run_ok()

        with patch.object(mod, "subprocess") as mock_sp:
            mock_sp.run.side_effect = mock_run
            mod._docker_rm_by_label("test-project")

        rm_cmds = [c for c in calls if "rm" in c]
        assert len(rm_cmds) == 3


class TestTryDropHostDb:
    def test_skips_invalid_db_name(self) -> None:
        mod = load_script("git_clean_them_all")
        with patch.object(mod, "subprocess") as mock_sp:
            mod._try_drop_host_db("invalid; DROP TABLE")
            mock_sp.run.assert_not_called()

    def test_drops_existing_db(self) -> None:
        mod = load_script("git_clean_them_all")
        calls: list[list[str]] = []

        def mock_run(cmd: list[str], **_kw: object) -> MagicMock:
            calls.append(cmd)
            if "psql" in cmd and any("SELECT 1" in c for c in cmd):
                return run_ok(stdout="1\n")
            return run_ok()

        with patch.object(mod, "subprocess") as mock_sp:
            mock_sp.run.side_effect = mock_run
            mod._try_drop_host_db("wt_42")

        dropdb_calls = [c for c in calls if c[0] == "dropdb"]
        assert len(dropdb_calls) == 1


class TestHasComposeFile:
    def test_detects_docker_compose_yml(self, tmp_path: Path) -> None:
        mod = load_script("git_clean_them_all")
        (tmp_path / "docker-compose.yml").touch()
        assert mod._has_compose_file(str(tmp_path)) is True

    def test_detects_compose_yml(self, tmp_path: Path) -> None:
        mod = load_script("git_clean_them_all")
        (tmp_path / "compose.yml").touch()
        assert mod._has_compose_file(str(tmp_path)) is True

    def test_returns_false_when_missing(self, tmp_path: Path) -> None:
        mod = load_script("git_clean_them_all")
        assert mod._has_compose_file(str(tmp_path)) is False


class TestCleanupDockerAndDb:
    def test_compose_down_without_project_name(self, tmp_path: Path) -> None:
        """Compose down works even when compose_project_name is empty."""
        mod = load_script("git_clean_them_all")
        ticket = tmp_path / "ticket"
        ticket.mkdir()
        wt = ticket / "repo"
        wt.mkdir()
        (wt / "docker-compose.yml").touch()
        # No .env.worktree → compose_project_name will be ""

        wt_info = mod.WorktreeInfo(
            repo="/fake/repo",
            wt_path=str(wt),
            wt_branch="feat",
            is_removable=True,
            dirty_reason="",
        )

        with patch.object(mod, "subprocess") as mock_sp:
            mock_sp.run.return_value = run_ok()
            mod._cleanup_docker_and_db(str(ticket), [wt_info], "99")

    def test_compose_down_for_worktree_with_compose_file(self, tmp_path: Path) -> None:
        mod = load_script("git_clean_them_all")
        ticket = tmp_path / "ticket"
        ticket.mkdir()
        wt = ticket / "repo"
        wt.mkdir()
        (wt / "docker-compose.yml").touch()
        (ticket / ".env.worktree").write_text("WT_DB_NAME=wt_99\nCOMPOSE_PROJECT_NAME=repo-wt99\n")

        wt_info = mod.WorktreeInfo(
            repo="/fake/repo",
            wt_path=str(wt),
            wt_branch="feat",
            is_removable=True,
            dirty_reason="",
        )
        compose_cmds: list[list[str]] = []

        def mock_run(cmd: list[str], **_kw: object) -> MagicMock:
            compose_cmds.append(cmd)
            return run_ok()

        with patch.object(mod, "subprocess") as mock_sp:
            mock_sp.run.side_effect = mock_run
            mod._cleanup_docker_and_db(str(ticket), [wt_info], "99")

        # Should have called docker compose down
        down_cmds = [c for c in compose_cmds if "down" in c]
        assert len(down_cmds) == 1
