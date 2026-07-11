"""Tests for teatree.core.intake.resolve — worktree resolution from CWD."""

from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.intake import resolve as resolve_module
from teatree.core.intake.resolve import (
    TicketIdentityCollisionError,
    WorkspaceOwnerCollisionError,
    WorktreeNotFoundError,
    WorktreePathConflictError,
    _auto_register_from_git,
    _find_env_cache,
    _parse_env_file,
    _refresh_reused_row,
    _ticket_by_number,
    _ticket_number_from_branch,
    _ticket_owning_branch,
    _warn_cwd_mismatch,
    _workspace_owner_ticket,
    match_worktree_by_path,
    resolve_worktree,
    tickets_owning_workspace_dir,
)
from teatree.core.models import Ticket, Worktree
from tests._git_repo import make_git_repo, run_git


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


def _write_cache(ticket_dir: Path, repo: str = "backend") -> Path:
    cache = ticket_dir / ".t3-cache" / repo / ".t3-env.cache"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("TICKET_DIR=/some/path\n", encoding="utf-8")
    return cache


class TestFindEnvCache:
    def test_found_in_worktree_sibling_from_cwd(self, tmp_path: Path) -> None:
        cache = _write_cache(tmp_path, "backend")
        worktree = tmp_path / "backend"
        worktree.mkdir()

        result = _find_env_cache(str(worktree))

        assert result == cache

    def test_found_walking_up_from_subdir(self, tmp_path: Path) -> None:
        cache = _write_cache(tmp_path, "backend")
        child = tmp_path / "backend" / "sub" / "deep"
        child.mkdir(parents=True)

        result = _find_env_cache(str(child))

        assert result == cache

    def test_sibling_repo_cache_is_not_returned(self, tmp_path: Path) -> None:
        """From inside one repo, the sibling repo's cache is never returned."""
        _write_cache(tmp_path, "frontend")
        backend = tmp_path / "backend"
        backend.mkdir()

        result = _find_env_cache(str(backend))

        assert result is None

    def test_not_found(self, tmp_path: Path) -> None:
        child = tmp_path / "a" / "b"
        child.mkdir(parents=True)

        result = _find_env_cache(str(child))

        assert result is None

    def test_broken_symlink_is_ignored(self, tmp_path: Path) -> None:
        """A broken symlink at the canonical path is not returned.

        ``is_file()`` is False for a dangling link, so a downstream
        ``read_text`` can never be handed a broken target.
        """
        cache_dir = tmp_path / ".t3-cache" / "backend"
        cache_dir.mkdir(parents=True)
        (cache_dir / ".t3-env.cache").symlink_to(tmp_path / "does-not-exist" / ".t3-env.cache")
        worktree = tmp_path / "backend"
        worktree.mkdir()

        result = _find_env_cache(str(worktree))

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

        result = match_worktree_by_path("/workspace/ac-backend-42/backend")

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

        result = match_worktree_by_path("/workspace/ac-backend-42/backend/src")

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

        result = match_worktree_by_path("/totally/different/path")

        assert result is None

    def test_skips_empty_extra(self) -> None:
        ticket = Ticket.objects.create()
        Worktree.objects.create(ticket=ticket, repo_path="backend", branch="feature", extra={})

        result = match_worktree_by_path("/some/path")

        assert result is None


class TestResolveWorktree(TestCase):
    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._monkeypatch = monkeypatch
        self._tmp_path = tmp_path

    def test_from_env_worktree_file(self) -> None:
        wt_dir = self._tmp_path / "backend"
        ticket = Ticket.objects.create()
        wt = Worktree.objects.create(
            ticket=ticket,
            repo_path="backend",
            branch="feature",
            extra={"worktree_path": str(wt_dir)},
        )

        envfile = self._tmp_path / ".t3-cache" / "backend" / ".t3-env.cache"
        envfile.parent.mkdir(parents=True, exist_ok=True)
        envfile.write_text(f"TICKET_DIR={wt_dir}\n", encoding="utf-8")
        self._monkeypatch.setenv("T3_ORIG_CWD", str(wt_dir))

        result = resolve_worktree()

        assert result.pk == wt.pk

    def test_stale_env_ticket_dir_falls_through_to_cwd_match(self) -> None:
        # 3e#3 nit (dropped `# pragma: no branch`): a stale env cache can name a
        # TICKET_DIR whose worktree row is gone. That miss must fall through to the
        # CWD-direct match (step 2), not treat the cache as truth or crash.
        cwd_dir = self._tmp_path / "backend"
        ticket = Ticket.objects.create()
        wt = Worktree.objects.create(
            ticket=ticket,
            repo_path="backend",
            branch="feature",
            extra={"worktree_path": str(cwd_dir)},
        )

        envfile = self._tmp_path / ".t3-cache" / "backend" / ".t3-env.cache"
        envfile.parent.mkdir(parents=True, exist_ok=True)
        # TICKET_DIR points at a path with NO matching Worktree row.
        envfile.write_text(f"TICKET_DIR={self._tmp_path / 'removed-worktree'}\n", encoding="utf-8")
        self._monkeypatch.setenv("T3_ORIG_CWD", str(cwd_dir))

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

        with patch("teatree.core.intake.resolve.git.current_branch", return_value="feat/branch"):
            result = resolve_worktree()

        assert result.branch == "feat/branch"
        assert result.repo_path == "my-repo"
        assert result.extra["worktree_path"] == str(wt_dir)

    def _auto_register_branch(self, branch: str) -> Ticket:
        wt_dir = self._tmp_path / "my-repo"
        wt_dir.mkdir()
        (wt_dir / ".git").write_text("gitdir: /some/main/.git/worktrees/my-repo\n")
        self._monkeypatch.setenv("T3_ORIG_CWD", str(wt_dir))
        with patch("teatree.core.intake.resolve.git.current_branch", return_value=branch):
            resolve_worktree()
        return Ticket.objects.get(issue_url=f"auto:{branch}")

    def test_auto_registered_fix_branch_ticket_is_fix(self) -> None:
        # #17: the auto-register intake site classifies from the branch name.
        assert self._auto_register_branch("fix/login-crash").kind == Ticket.Kind.FIX

    def test_auto_registered_feature_branch_ticket_is_feature(self) -> None:
        assert self._auto_register_branch("feat/dark-mode").kind == Ticket.Kind.FEATURE

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
        """When .t3-env.cache exists but has no TICKET_DIR, fall through to CWD match."""
        cwd = self._tmp_path / "backend"
        envfile = self._tmp_path / ".t3-cache" / "backend" / ".t3-env.cache"
        envfile.parent.mkdir(parents=True, exist_ok=True)
        envfile.write_text("SOME_OTHER_KEY=value\n", encoding="utf-8")
        self._monkeypatch.setenv("T3_ORIG_CWD", str(cwd))

        with pytest.raises(WorktreeNotFoundError):
            resolve_worktree()


class TestResolveWorktreeRejectsMainClone(TestCase):
    """The main-clone refusal must apply to every return path (#752).

    A stale or mis-recorded env cache whose ``TICKET_DIR`` resolves to a
    ``Worktree`` row pointing at a *main clone* must be rejected by the
    same guard step 2 uses — not handed back as a usable worktree, which
    would route destructive consumers (db reset, teardown, cleanup) at
    the main clone.
    """

    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._monkeypatch = monkeypatch
        self._tmp_path = tmp_path

    def _make_main_clone(self, name: str) -> Path:
        """A main clone has ``.git`` as a directory (not a file)."""
        clone = self._tmp_path / name
        clone.mkdir()
        (clone / ".git").mkdir()
        return clone

    def test_step1_env_cache_pointing_at_main_clone_is_rejected(self) -> None:
        """Step 1 (env-cache TICKET_DIR) must hit the main-clone guard.

        RED before #752: step 1 returns the main-clone Worktree with no
        ``WorktreeNotFoundError`` (the guard only ran on step 2).
        """
        main_clone = self._make_main_clone("teatree")
        ticket = Ticket.objects.create()
        Worktree.objects.create(
            ticket=ticket,
            repo_path="teatree",
            branch="main",
            extra={"worktree_path": str(main_clone)},
        )

        # CWD is elsewhere; an env cache there points TICKET_DIR at the
        # main clone (the stale/mis-recorded condition the guard catches).
        cwd = self._tmp_path / "elsewhere"
        cwd.mkdir()
        envfile = cwd.parent / ".t3-cache" / cwd.name / ".t3-env.cache"
        envfile.parent.mkdir(parents=True, exist_ok=True)
        envfile.write_text(f"TICKET_DIR={main_clone}\n", encoding="utf-8")
        self._monkeypatch.setenv("T3_ORIG_CWD", str(cwd))

        with pytest.raises(WorktreeNotFoundError, match="Refusing to operate on main clone"):
            resolve_worktree()

    def test_step2_main_clone_guard_still_fires(self) -> None:
        """Step 2 keeps refusing a main clone after the guard is shared."""
        main_clone = self._make_main_clone("teatree")
        ticket = Ticket.objects.create()
        Worktree.objects.create(
            ticket=ticket,
            repo_path="teatree",
            branch="main",
            extra={"worktree_path": str(main_clone)},
        )
        self._monkeypatch.setenv("T3_ORIG_CWD", str(main_clone))

        with pytest.raises(WorktreeNotFoundError, match="Refusing to operate on main clone"):
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

        with self._caplog.at_level("WARNING", logger="teatree.core.intake.resolve"):
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

        with self._caplog.at_level("WARNING", logger="teatree.core.intake.resolve"):
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

        with self._caplog.at_level("WARNING", logger="teatree.core.intake.resolve"):
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

        with self._caplog.at_level("WARNING", logger="teatree.core.intake.resolve"):
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

        with self._caplog.at_level("WARNING", logger="teatree.core.intake.resolve"):
            _warn_cwd_mismatch(wt, "/some/path")

        assert not self._caplog.records


class TestAutoRegisterFromGit:
    def test_returns_none_when_git_fails(self, tmp_path: Path) -> None:
        """Git command failure should return None, not raise."""
        wt_dir = tmp_path / "my-repo"
        wt_dir.mkdir()
        (wt_dir / ".git").write_text("gitdir: /some/.git/worktrees/my-repo\n")

        with patch("teatree.core.intake.resolve.git.current_branch", return_value=""):
            assert _auto_register_from_git(str(wt_dir)) is None

    def test_returns_none_when_branch_empty(self, tmp_path: Path) -> None:
        """Detached HEAD (empty branch) should return None."""
        wt_dir = tmp_path / "my-repo"
        wt_dir.mkdir()
        (wt_dir / ".git").write_text("gitdir: /some/.git/worktrees/my-repo\n")

        with patch("teatree.core.intake.resolve.git.current_branch", return_value=""):
            assert _auto_register_from_git(str(wt_dir)) is None


class TestAutoRegisterReusesExistingWorktree(TestCase):
    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def _make_git_worktree(self, name: str) -> Path:
        wt_dir = self._tmp_path / name
        wt_dir.mkdir()
        (wt_dir / ".git").write_text(f"gitdir: /some/.git/worktrees/{name}\n")
        return wt_dir

    def test_reuses_existing_worktree_for_same_branch_and_repo(self) -> None:
        """An existing Worktree for this branch+repo should be reused, not duplicated.

        Bug: ``workspace ticket <real_url>`` provisions a Worktree row keyed
        to a real issue URL. Running ``t3`` from inside that worktree dropped
        through to ``_auto_register_from_git`` whenever
        ``match_worktree_by_path`` failed (e.g., the stored ``worktree_path``
        was missing or differed by a trailing slash), creating a duplicate
        ``auto:<branch>`` ticket. The fix: look up the Worktree by branch+repo
        first, and only fall through to ticket creation when nothing exists.
        """
        ticket = Ticket.objects.create(issue_url="https://gitlab.com/org/repo/-/issues/123")
        existing = Worktree.objects.create(
            ticket=ticket,
            repo_path="my-repo",
            branch="feat/branch",
            extra={},  # worktree_path was never recorded — that's why match_worktree_by_path missed it
        )
        wt_dir = self._make_git_worktree("my-repo")

        with patch("teatree.core.intake.resolve.git.current_branch", return_value="feat/branch"):
            result = _auto_register_from_git(str(wt_dir))

        assert result is not None
        assert result.pk == existing.pk
        assert result.ticket_id == ticket.pk
        assert Ticket.objects.count() == 1
        assert not Ticket.objects.filter(issue_url="auto:feat/branch").exists()

    def test_backfills_missing_worktree_path_on_reuse(self) -> None:
        """Reusing a Worktree should backfill ``extra.worktree_path``.

        After this fix, future calls to ``match_worktree_by_path`` will
        match the same Worktree directly (step 2 of resolution) without
        needing to fall through to auto-register again.
        """
        ticket = Ticket.objects.create(issue_url="https://gitlab.com/org/repo/-/issues/123")
        Worktree.objects.create(
            ticket=ticket,
            repo_path="my-repo",
            branch="feat/branch",
            extra={"some_other": "value"},
        )
        wt_dir = self._make_git_worktree("my-repo")

        with patch("teatree.core.intake.resolve.git.current_branch", return_value="feat/branch"):
            result = _auto_register_from_git(str(wt_dir))

        assert result is not None
        assert result.extra["worktree_path"] == str(wt_dir)
        assert result.extra["some_other"] == "value"

    def test_ticket_reuse_refreshes_stale_branch_and_path(self) -> None:
        """Ticket+repo reuse must re-point the row at the worktree being resolved.

        Bug: when a ticket already had a Worktree row for this repo (created
        for an EARLIER worktree dir/branch of the same ticket),
        ``get_or_create`` returned it with its defaults ignored — the row kept
        the stale ``branch`` and ``extra.worktree_path``. Every downstream
        consumer then acted on the stale directory: a frontend build invoked
        from the new worktree silently ran inside the old directory.
        """
        ticket = Ticket.objects.create(issue_url="https://gitlab.com/org/repo/-/issues/321")
        stale = Worktree.objects.create(
            ticket=ticket,
            repo_path="my-repo",
            branch="feat/old-branch",
            extra={"worktree_path": "/somewhere/else/my-repo", "keep": "me"},
        )
        wt_dir = self._make_git_worktree("my-repo")

        with patch("teatree.core.intake.resolve.git.current_branch", return_value="feat/new-branch"):
            result = _auto_register_from_git(str(wt_dir), ticket_hint=ticket)

        assert result is not None
        assert result.pk == stale.pk
        assert result.branch == "feat/new-branch"
        assert result.extra["worktree_path"] == str(wt_dir)
        assert result.extra["keep"] == "me"
        assert Worktree.objects.count() == 1

    def test_creates_new_ticket_when_no_worktree_exists(self) -> None:
        """First-time auto-register still creates a fresh ``auto:<branch>`` ticket."""
        wt_dir = self._make_git_worktree("my-repo")

        with patch("teatree.core.intake.resolve.git.current_branch", return_value="feat/new"):
            result = _auto_register_from_git(str(wt_dir))

        assert result is not None
        assert result.branch == "feat/new"
        assert result.repo_path == "my-repo"
        assert result.extra["worktree_path"] == str(wt_dir)
        assert result.ticket.issue_url == "auto:feat/new"

    def test_new_worktree_inherits_ticket_overlay(self) -> None:
        """A newly auto-registered worktree gets ``overlay`` from its ticket (#1397).

        Bug: ``get_or_create`` did not set ``overlay`` in its defaults, so a
        cwd-auto-detected worktree was created with ``Worktree.overlay=''`` even
        when its ticket carried the real overlay — which let the per-overlay
        ``max_concurrent_local_stacks`` gate miss the row and breach the cap.
        """
        ticket = Ticket.objects.create(
            issue_url="https://example.com/t3-heavy/issues/777",
            overlay="t3-heavy",
        )
        wt_dir = self._make_git_worktree("my-repo")

        with patch("teatree.core.intake.resolve.git.current_branch", return_value="feat/branch"):
            result = _auto_register_from_git(str(wt_dir), ticket_hint=ticket)

        assert result is not None
        assert result.ticket_id == ticket.pk
        assert result.overlay == "t3-heavy"

    def test_does_not_match_worktree_for_different_repo(self) -> None:
        """A Worktree for the same branch but a different repo must not be reused."""
        ticket = Ticket.objects.create(issue_url="https://gitlab.com/org/repo/-/issues/123")
        Worktree.objects.create(
            ticket=ticket,
            repo_path="other-repo",
            branch="feat/branch",
            extra={"worktree_path": "/tmp/other-repo"},
        )
        wt_dir = self._make_git_worktree("my-repo")

        with patch("teatree.core.intake.resolve.git.current_branch", return_value="feat/branch"):
            result = _auto_register_from_git(str(wt_dir))

        assert result is not None
        assert result.repo_path == "my-repo"
        assert result.ticket.issue_url == "auto:feat/branch"
        assert Worktree.objects.count() == 2

    def test_reuses_workspace_owner_ticket_for_sibling_branch(self) -> None:
        """A sibling worktree under the same workspace dir reuses its ticket (#641).

        Workspace ``ticket <real_url>`` owns repoA on branch A. Resolving a
        *different* repo/branch under the SAME workspace dir must attach to
        that ticket, not fork a new ``auto:<branch>`` ticket.
        """
        owner_ticket = Ticket.objects.create(issue_url="https://gitlab.com/org/repo/-/issues/94")
        repo_a = self._make_git_worktree("repo-a")
        Worktree.objects.create(
            ticket=owner_ticket,
            repo_path="repo-a",
            branch="ac/real-work",
            extra={"worktree_path": str(repo_a)},
        )
        repo_b = self._make_git_worktree("repo-b")  # sibling under the same tmp_path

        with patch("teatree.core.intake.resolve.git.current_branch", return_value="feat/other"):
            result = _auto_register_from_git(str(repo_b))

        assert result is not None
        assert result.ticket_id == owner_ticket.pk
        assert result.repo_path == "repo-b"
        assert result.branch == "feat/other"
        assert not Ticket.objects.filter(issue_url="auto:feat/other").exists()
        assert Ticket.objects.count() == 1

    def test_reuses_workspace_owner_when_stored_path_is_symlink_unresolved(self) -> None:
        """Owner is found even when the stored path is the unresolved (symlinked) form.

        Provision records ``worktree_path`` verbatim from
        ``config.worktree_root()`` (no ``.resolve()``), while resolution
        ``.resolve()``-s cwd. On a symlinked workspace root (macOS
        ``/tmp`` → ``/private/tmp``) the two forms differ; the match must
        still succeed via ``_candidate_paths``.
        """
        owner_ticket = Ticket.objects.create(issue_url="https://gitlab.com/org/repo/-/issues/94")
        repo_a = self._make_git_worktree("repo-a")
        # Simulate the unresolved stored form by de-privatising the path
        # the way an unresolved macOS workspace root would be recorded.
        stored = str(repo_a).replace("/private/", "/", 1) if str(repo_a).startswith("/private/") else str(repo_a)
        Worktree.objects.create(
            ticket=owner_ticket,
            repo_path="repo-a",
            branch="ac/real-work",
            extra={"worktree_path": stored},
        )
        repo_b = self._make_git_worktree("repo-b")

        with patch("teatree.core.intake.resolve.git.current_branch", return_value="feat/other"):
            result = _auto_register_from_git(str(repo_b))

        assert result is not None
        assert result.ticket_id == owner_ticket.pk
        assert not Ticket.objects.filter(issue_url="auto:feat/other").exists()

    def test_no_workspace_owner_still_creates_auto_ticket(self) -> None:
        """When no sibling worktree shares the workspace dir, the auto: path stands."""
        wt_dir = self._make_git_worktree("lonely-repo")

        with patch("teatree.core.intake.resolve.git.current_branch", return_value="feat/solo"):
            result = _auto_register_from_git(str(wt_dir))

        assert result is not None
        assert result.ticket.issue_url == "auto:feat/solo"


class TestTicketNumberFromBranch:
    """Parse the ticket number that ``build_branch_name`` encodes in a branch.

    ``workspace ticket`` names branches ``<number>-<slug>`` (see
    ``_workspace_ticket_intake.build_branch_name``). Older/manual branches also use a
    ``<scope>/<number>-<slug>`` shape. The parser must recover ``<number>``
    from both so a manually-added worktree attaches to the right ticket.
    """

    def test_flat_number_slug(self) -> None:
        assert _ticket_number_from_branch("1180-fix-the-thing") == "1180"

    def test_scoped_number_slug(self) -> None:
        assert _ticket_number_from_branch("ac/1180-fix-the-thing") == "1180"

    def test_bare_number(self) -> None:
        assert _ticket_number_from_branch("1180") == "1180"

    def test_no_leading_number_returns_none(self) -> None:
        assert _ticket_number_from_branch("feat/some-feature") is None

    def test_no_number_at_all_returns_none(self) -> None:
        assert _ticket_number_from_branch("main") is None

    def test_empty_returns_none(self) -> None:
        assert _ticket_number_from_branch("") is None

    def test_does_not_match_number_buried_after_words(self) -> None:
        """The number must be the leading segment, not any digits in the slug."""
        assert _ticket_number_from_branch("feature-1180-thing") is None


class TestTicketOwningBranch(TestCase):
    """Match a branch to the ticket whose ``ticket_number`` it encodes."""

    def test_matches_ticket_by_trailing_issue_number(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://github.com/org/repo/issues/1180")

        assert _ticket_owning_branch("1180-fix-the-thing") == ticket

    def test_returns_none_when_no_ticket_has_that_number(self) -> None:
        Ticket.objects.create(issue_url="https://github.com/org/repo/issues/42")

        assert _ticket_owning_branch("1180-fix-the-thing") is None

    def test_returns_none_when_branch_has_no_number(self) -> None:
        Ticket.objects.create(issue_url="https://github.com/org/repo/issues/1180")

        assert _ticket_owning_branch("feat/no-number") is None


class TestAutoRegisterAttributesManualWorktreeToBranchTicket(TestCase):
    """A manually-added worktree must attach to the ticket its branch encodes.

    Bug: ``git worktree add`` (no ``workspace ticket``) drops through
    ``_auto_register_from_git``. With no matching Worktree row, it used to
    call ``_workspace_owner_ticket``, which keys on the parent directory and
    grabs the most-recent *sibling* ticket — misattributing the worktree to
    an unrelated ticket. The branch name already carries the ticket number
    (``<number>-<slug>``), so resolution must prefer the ticket that number
    identifies.
    """

    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def _make_git_worktree(self, name: str) -> Path:
        wt_dir = self._tmp_path / name
        wt_dir.mkdir()
        (wt_dir / ".git").write_text(f"gitdir: /some/.git/worktrees/{name}\n")
        return wt_dir

    def test_branch_ticket_wins_over_sibling_workspace_owner(self) -> None:
        # A sibling worktree for an unrelated ticket already lives under the
        # same parent dir — the misattribution trap.
        sibling_ticket = Ticket.objects.create(issue_url="https://github.com/org/repo/issues/999")
        sibling = self._make_git_worktree("sibling-repo")
        Worktree.objects.create(
            ticket=sibling_ticket,
            repo_path="sibling-repo",
            branch="999-unrelated",
            extra={"worktree_path": str(sibling)},
        )
        # The real ticket the manual worktree belongs to.
        real_ticket = Ticket.objects.create(issue_url="https://github.com/org/repo/issues/1180")
        manual = self._make_git_worktree("manual-repo")

        with patch("teatree.core.intake.resolve.git.current_branch", return_value="1180-fix-the-thing"):
            result = _auto_register_from_git(str(manual))

        assert result is not None
        assert result.ticket_id == real_ticket.pk
        assert result.ticket_id != sibling_ticket.pk
        assert not Ticket.objects.filter(issue_url__startswith="auto:").exists()

    def test_falls_back_to_auto_ticket_when_no_branch_ticket_and_no_owner(self) -> None:
        manual = self._make_git_worktree("manual-repo")

        with patch("teatree.core.intake.resolve.git.current_branch", return_value="1180-fix-the-thing"):
            result = _auto_register_from_git(str(manual))

        assert result is not None
        assert result.ticket.issue_url == "auto:1180-fix-the-thing"


class TestResolveWorktreeTicketHint(TestCase):
    """``ticket_hint`` rebinds a synthetic-ticket worktree but never steals a real one."""

    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._monkeypatch = monkeypatch
        self._tmp_path = tmp_path

    def test_rebinds_worktree_stuck_on_auto_ticket(self) -> None:
        auto_ticket = Ticket.objects.create(issue_url="auto:some-branch")
        wt_path = str(self._tmp_path / "backend")
        wt = Worktree.objects.create(
            ticket=auto_ticket,
            repo_path="backend",
            branch="some-branch",
            extra={"worktree_path": wt_path},
        )
        real_ticket = Ticket.objects.create(issue_url="https://github.com/org/repo/issues/321")
        self._monkeypatch.setenv("T3_ORIG_CWD", wt_path)

        result = resolve_worktree(ticket_hint=real_ticket)

        assert result.pk == wt.pk
        result.refresh_from_db()
        assert result.ticket_id == real_ticket.pk

    def test_noop_when_hint_already_the_current_ticket(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://github.com/org/repo/issues/77")
        wt_path = str(self._tmp_path / "backend")
        wt = Worktree.objects.create(
            ticket=ticket,
            repo_path="backend",
            branch="77-x",
            extra={"worktree_path": wt_path},
        )
        self._monkeypatch.setenv("T3_ORIG_CWD", wt_path)

        result = resolve_worktree(ticket_hint=ticket)

        assert result.pk == wt.pk
        result.refresh_from_db()
        assert result.ticket_id == ticket.pk

    def test_leaves_worktree_on_real_ticket_untouched(self) -> None:
        real_ticket = Ticket.objects.create(issue_url="https://github.com/org/repo/issues/55")
        wt_path = str(self._tmp_path / "backend")
        wt = Worktree.objects.create(
            ticket=real_ticket,
            repo_path="backend",
            branch="55-real",
            extra={"worktree_path": wt_path},
        )
        other_ticket = Ticket.objects.create(issue_url="https://github.com/org/repo/issues/999")
        self._monkeypatch.setenv("T3_ORIG_CWD", wt_path)

        result = resolve_worktree(ticket_hint=other_ticket)

        assert result.pk == wt.pk
        result.refresh_from_db()
        assert result.ticket_id == real_ticket.pk


class TestWorkspaceOwnerFailsLoudOnCollision(TestCase):
    """Multi-owner workspace dir fails loud, never picks an arbitrary ticket (#WT-PR-D finding 11)."""

    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def _seed_two_owners(self) -> tuple[Ticket, Ticket]:
        repo_a = self._tmp_path / "repo-a"
        repo_b = self._tmp_path / "repo-b"
        repo_a.mkdir()
        repo_b.mkdir()
        first_ticket = Ticket.objects.create(issue_url="https://gitlab.com/org/repo/-/issues/1")
        second_ticket = Ticket.objects.create(issue_url="https://gitlab.com/org/repo/-/issues/2")
        Worktree.objects.create(
            ticket=first_ticket,
            repo_path="repo-a",
            branch="ac/first",
            extra={"worktree_path": str(repo_a)},
        )
        Worktree.objects.create(
            ticket=second_ticket,
            repo_path="repo-b",
            branch="ac/second",
            extra={"worktree_path": str(repo_b)},
        )
        return first_ticket, second_ticket

    def test_workspace_owner_ticket_raises_on_multiple_owners(self) -> None:
        """A workspace dir holding two tickets' worktrees raises, not an arbitrary pick.

        The pre-fix code returned the lowest-pk owner silently; that
        arbitrary selection is exactly what mis-attributes a sibling worktree.
        """
        self._seed_two_owners()

        with pytest.raises(WorkspaceOwnerCollisionError):
            _workspace_owner_ticket((self._tmp_path / "repo-c").resolve())

    def test_tickets_owning_workspace_dir_lists_all_owners(self) -> None:
        first_ticket, second_ticket = self._seed_two_owners()

        owners = tickets_owning_workspace_dir(self._tmp_path.resolve())

        assert {t.pk for t in owners} == {first_ticket.pk, second_ticket.pk}

    def test_single_owner_resolves_without_raising(self) -> None:
        repo_a = self._tmp_path / "repo-a"
        repo_a.mkdir()
        ticket = Ticket.objects.create(issue_url="https://gitlab.com/org/repo/-/issues/1")
        Worktree.objects.create(
            ticket=ticket,
            repo_path="repo-a",
            branch="ac/first",
            extra={"worktree_path": str(repo_a)},
        )

        owner = _workspace_owner_ticket((self._tmp_path / "repo-c").resolve())

        assert owner is not None
        assert owner.pk == ticket.pk


class TestTicketByNumberFailsLoudOnCollision(TestCase):
    """A non-unique ``ticket_number`` fails loud, never resolves an arbitrary ticket (#WT-PR-D finding 9)."""

    def test_raises_when_two_tickets_share_number(self) -> None:
        Ticket.objects.create(issue_url="https://a.example.com/x/issues/5")
        Ticket.objects.create(issue_url="https://b.example.com/y/issues/5")

        with pytest.raises(TicketIdentityCollisionError):
            _ticket_by_number("5")

    def test_returns_single_match(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://a.example.com/x/issues/5")

        assert _ticket_by_number("5") == ticket

    def test_returns_none_when_no_match(self) -> None:
        Ticket.objects.create(issue_url="https://a.example.com/x/issues/42")

        assert _ticket_by_number("5") is None

    def test_overlay_scope_disambiguates_cross_overlay_collision(self) -> None:
        Ticket.objects.create(issue_url="https://a.example.com/x/issues/5", overlay="ov-a")
        ticket_b = Ticket.objects.create(issue_url="https://b.example.com/y/issues/5", overlay="ov-b")

        assert _ticket_by_number("5", overlay="ov-b") == ticket_b

    def test_branch_lookup_raises_on_collision(self) -> None:
        Ticket.objects.create(issue_url="https://a.example.com/x/issues/5")
        Ticket.objects.create(issue_url="https://b.example.com/y/issues/5")

        with pytest.raises(TicketIdentityCollisionError):
            _ticket_owning_branch("5-fix-the-thing")

    def test_blank_number_returns_none_never_fans_out_to_pk_fallback_rows(self) -> None:
        # A blank hint's empty ``issue_number`` filter would otherwise match EVERY
        # pk-fallback ticket (issue_url with no trailing number → blank
        # ``issue_number``) and then fail loud as a false collision.
        Ticket.objects.create(issue_url="https://a.example.com/x/notes")
        Ticket.objects.create(issue_url="https://b.example.com/y/wiki")

        assert _ticket_by_number("") is None


class TestRefreshReusedRowRefusesPathSteal(TestCase):
    """A reused row must never be repointed onto a path another row owns (#WT-PR-D finding 8)."""

    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def test_raises_when_target_path_owned_by_another_row(self) -> None:
        owned_path = str(self._tmp_path / "shared-dir")
        ticket_a = Ticket.objects.create(issue_url="https://a.example.com/x/issues/1")
        ticket_b = Ticket.objects.create(issue_url="https://b.example.com/y/issues/2")
        Worktree.objects.create(
            ticket=ticket_a,
            repo_path="repo",
            branch="1-x",
            extra={"worktree_path": owned_path},
        )
        row_b = Worktree.objects.create(
            ticket=ticket_b,
            repo_path="repo",
            branch="2-x",
            extra={"worktree_path": str(self._tmp_path / "b-dir")},
        )

        with pytest.raises(WorktreePathConflictError):
            _refresh_reused_row(row_b, "2-x", Path(owned_path))

    def test_refreshes_freely_when_target_path_unowned(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://a.example.com/x/issues/1")
        row = Worktree.objects.create(
            ticket=ticket,
            repo_path="repo",
            branch="1-old",
            extra={"worktree_path": str(self._tmp_path / "old-dir"), "keep": "me"},
        )
        new_dir = self._tmp_path / "new-dir"

        _refresh_reused_row(row, "1-new", new_dir)

        row.refresh_from_db()
        assert row.branch == "1-new"
        assert row.extra["worktree_path"] == str(new_dir)
        assert row.extra["keep"] == "me"


class TestResolveModuleDocstringMatchesCacheLocation:
    """Guard the env-cache resolution docs against a stale mental model.

    The env cache is the out-of-repo ``.t3-cache/`` sibling of the worktree,
    never copied into a repo tree (#3097) and never a symlink. The module
    docstring, the ``_find_env_cache`` docstring, and the inline comment in
    ``resolve_worktree`` must describe it that way — otherwise a reader
    chasing an untracked-file or bind-mount failure is led back to the
    pre-fix mental model.

    Anti-vacuous: revert any of these sites to the word "symlink" and the
    relevant assertion goes red.
    """

    def _resolve_source(self) -> str:
        return Path(resolve_module.__file__).read_text(encoding="utf-8")

    def test_module_docstring_does_not_call_env_cache_a_symlink(self) -> None:
        docstring = resolve_module.__doc__ or ""
        assert "symlink" not in docstring.lower(), (
            "module docstring still describes the env cache as a symlink; "
            "since #3097 it is the out-of-repo .t3-cache/ sibling"
        )
        assert "env cache" in docstring, (
            "module docstring must still mention the env cache as the first resolution anchor"
        )

    def test_find_env_cache_docstring_does_not_mention_symlink(self) -> None:
        docstring = _find_env_cache.__doc__ or ""
        assert "symlink" not in docstring.lower(), (
            "_find_env_cache docstring still describes the env cache as a "
            "symlink; since #3097 it is the out-of-repo .t3-cache/ sibling"
        )

    def test_resolve_worktree_step1_comment_does_not_mention_symlink(self) -> None:
        source = self._resolve_source()
        marker = "# 1. Walk up from CWD to the .t3-cache/ sibling"
        assert marker in source, "step-1 walk-up comment moved or was removed"
        start = source.index(marker)
        block = source[start : start + 200]
        assert "symlink" not in block.lower(), (
            f"step-1 inline comment in resolve_worktree still mentions "
            f"'symlink'; since #3097 the env cache is the out-of-repo "
            f".t3-cache/ sibling. Offending block:\n{block}"
        )


class TestProvisionResolveAttributionRoundTrip(TestCase):
    """Real git worktrees under a symlinked workspace root resolve to the right ticket.

    The headline integration net for the cross-attach bug class (#WT-PR-D): a
    REAL ``git worktree add`` under a workspace root reached via a symlink
    (mimicking macOS ``/tmp`` → ``/private/tmp``) must attribute to the ticket
    its branch encodes — never to a FOREIGN merged ticket whose lingering row
    merely shares the branch + repo basename. A second sibling worktree under
    the same ticket dir must attach to that same ticket through the symlink,
    not fork a fresh ``auto:<branch>`` ticket.

    RED before the fix: the auto-register reuse arm ran BEFORE attribution, so
    both worktrees bound to the foreign merged ticket #999.
    """

    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._monkeypatch = monkeypatch
        self._tmp_path = tmp_path

    def test_real_worktrees_attribute_to_provisioned_ticket_not_foreign_merged(self) -> None:
        real_root = self._tmp_path / "real"
        real_root.mkdir()
        repo_a_clone = make_git_repo(real_root / "repo-a")
        repo_b_clone = make_git_repo(real_root / "repo-b")

        # Workspace root reached through a symlink (macOS /tmp → /private/tmp).
        link_root = self._tmp_path / "wslink"
        link_root.symlink_to(real_root)

        provisioned = Ticket.objects.create(issue_url="https://github.com/org/repo/issues/123")
        foreign_merged = Ticket.objects.create(issue_url="https://github.com/org/repo/issues/999")
        # The lingering merged-ticket row that shares the branch + repo basename
        # — the cross-attach source. Its stored path is unrelated/stale.
        foreign_row = Worktree.objects.create(
            ticket=foreign_merged,
            repo_path="repo-a",
            branch="123-feature",
            extra={"worktree_path": "/stale/merged/repo-a"},
        )

        ticket_dir = link_root / "123-feature"
        ticket_dir.mkdir()
        run_git(repo_a_clone, "worktree", "add", "-b", "123-feature", str(ticket_dir / "repo-a"))
        run_git(repo_b_clone, "worktree", "add", "-b", "feat/sibling", str(ticket_dir / "repo-b"))

        resolved_cwd_a = str((ticket_dir / "repo-a").resolve())
        self._monkeypatch.setenv("T3_ORIG_CWD", resolved_cwd_a)
        wt_a = resolve_worktree()

        # Branch-encoded number 123 wins over the foreign #999 row.
        assert wt_a.ticket_id == provisioned.pk
        # The foreign merged row is never stolen / repointed.
        foreign_row.refresh_from_db()
        assert foreign_row.extra["worktree_path"] == "/stale/merged/repo-a"

        resolved_cwd_b = str((ticket_dir / "repo-b").resolve())
        self._monkeypatch.setenv("T3_ORIG_CWD", resolved_cwd_b)
        wt_b = resolve_worktree()

        # The non-numbered sibling attaches to the SAME ticket via the
        # symlink-tolerant workspace-owner resolver, not a fresh auto: ticket.
        assert wt_b.ticket_id == provisioned.pk
        assert wt_b.repo_path == "repo-b"
        assert not Ticket.objects.filter(issue_url="auto:feat/sibling").exists()
