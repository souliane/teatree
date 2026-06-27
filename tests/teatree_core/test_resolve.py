"""Tests for teatree.core.resolve — worktree resolution from CWD."""

from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core import resolve as resolve_module
from teatree.core.models import Ticket, Worktree
from teatree.core.resolve import (
    WorktreeNotFoundError,
    _auto_register_from_git,
    _find_env_cache,
    _parse_env_file,
    _ticket_number_from_branch,
    _ticket_owning_branch,
    _warn_cwd_mismatch,
    _workspace_owner_ticket,
    match_worktree_by_path,
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


class TestFindEnvCache:
    def test_found_in_cwd(self, tmp_path: Path) -> None:
        envfile = tmp_path / ".t3-env.cache"
        envfile.write_text("TICKET_DIR=/some/path\n", encoding="utf-8")

        result = _find_env_cache(str(tmp_path))

        assert result == envfile

    def test_found_in_parent(self, tmp_path: Path) -> None:
        envfile = tmp_path / ".t3-env.cache"
        envfile.write_text("TICKET_DIR=/some/path\n", encoding="utf-8")
        child = tmp_path / "sub" / "deep"
        child.mkdir(parents=True)

        result = _find_env_cache(str(child))

        assert result == envfile

    def test_not_found(self, tmp_path: Path) -> None:
        child = tmp_path / "a" / "b"
        child.mkdir(parents=True)

        result = _find_env_cache(str(child))

        assert result is None

    def test_broken_symlink_is_ignored(self, tmp_path: Path) -> None:
        """Broken symlinks are not returned.

        Under a Docker mount that does not include the ticket dir, the
        worktree's ``.t3-env.cache`` symlink stays present but its target
        disappears; returning it would crash downstream ``read_text``.
        """
        link = tmp_path / ".t3-env.cache"
        link.symlink_to(tmp_path / "does-not-exist" / ".t3-env.cache")

        result = _find_env_cache(str(tmp_path))

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
        ticket = Ticket.objects.create()
        wt = Worktree.objects.create(
            ticket=ticket,
            repo_path="backend",
            branch="feature",
            extra={"worktree_path": str(self._tmp_path / "ticket-dir")},
        )

        envfile = self._tmp_path / ".t3-env.cache"
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
        """When .t3-env.cache exists but has no TICKET_DIR, fall through to CWD match."""
        envfile = self._tmp_path / ".t3-env.cache"
        envfile.write_text("SOME_OTHER_KEY=value\n", encoding="utf-8")
        self._monkeypatch.setenv("T3_ORIG_CWD", str(self._tmp_path))

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
        envfile = cwd / ".t3-env.cache"
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

        with patch("teatree.core.resolve.git.current_branch", return_value="feat/branch"):
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

        with patch("teatree.core.resolve.git.current_branch", return_value="feat/branch"):
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

        with patch("teatree.core.resolve.git.current_branch", return_value="feat/new-branch"):
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

        with patch("teatree.core.resolve.git.current_branch", return_value="feat/new"):
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

        with patch("teatree.core.resolve.git.current_branch", return_value="feat/branch"):
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

        with patch("teatree.core.resolve.git.current_branch", return_value="feat/branch"):
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

        with patch("teatree.core.resolve.git.current_branch", return_value="feat/other"):
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

        with patch("teatree.core.resolve.git.current_branch", return_value="feat/other"):
            result = _auto_register_from_git(str(repo_b))

        assert result is not None
        assert result.ticket_id == owner_ticket.pk
        assert not Ticket.objects.filter(issue_url="auto:feat/other").exists()

    def test_no_workspace_owner_still_creates_auto_ticket(self) -> None:
        """When no sibling worktree shares the workspace dir, the auto: path stands."""
        wt_dir = self._make_git_worktree("lonely-repo")

        with patch("teatree.core.resolve.git.current_branch", return_value="feat/solo"):
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

        with patch("teatree.core.resolve.git.current_branch", return_value="1180-fix-the-thing"):
            result = _auto_register_from_git(str(manual))

        assert result is not None
        assert result.ticket_id == real_ticket.pk
        assert result.ticket_id != sibling_ticket.pk
        assert not Ticket.objects.filter(issue_url__startswith="auto:").exists()

    def test_falls_back_to_auto_ticket_when_no_branch_ticket_and_no_owner(self) -> None:
        manual = self._make_git_worktree("manual-repo")

        with patch("teatree.core.resolve.git.current_branch", return_value="1180-fix-the-thing"):
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


class TestWorkspaceOwnerTicketIsDeterministic(TestCase):
    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def test_lowest_pk_wins_when_multiple_siblings_share_workspace(self) -> None:
        """Resolution stays deterministic if the invariant is violated.

        The one-ticket-per-workspace invariant being violated, the
        lowest-``pk`` worktree's ticket wins. Without ``.order_by("pk")``
        the "first match wins" comment is a lie — the unordered queryset's
        iteration order is backend-dependent.
        """
        repo_a = self._tmp_path / "repo-a"
        repo_b = self._tmp_path / "repo-b"
        repo_a.mkdir()
        repo_b.mkdir()

        first_ticket = Ticket.objects.create(issue_url="https://gitlab.com/org/repo/-/issues/1")
        second_ticket = Ticket.objects.create(issue_url="https://gitlab.com/org/repo/-/issues/2")
        first_wt = Worktree.objects.create(
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

        # Resolving a third sibling under the same workspace dir: cwd's
        # parent is tmp_path, matching both stored worktree_path parents.
        owner = _workspace_owner_ticket((self._tmp_path / "repo-c").resolve())

        assert owner is not None
        assert owner.pk == first_ticket.pk
        assert owner.pk == first_wt.ticket.pk


class TestResolveModuleDocstringMatchesCopyShape:
    """Guard against re-introducing the pre-#1316 "symlink" wording.

    The in-worktree env cache is a regular file copy (since #1316), not a
    symlink. The module-level docstring, the ``_find_env_cache`` docstring,
    and the inline comment in ``resolve_worktree`` must describe it that
    way — otherwise readers chasing a Docker bind-mount failure will be
    led back to the pre-fix mental model.

    Anti-vacuous: revert any of these sites to the word "symlink" and the
    relevant assertion goes red.
    """

    def _resolve_source(self) -> str:
        return Path(resolve_module.__file__).read_text(encoding="utf-8")

    def test_module_docstring_does_not_call_env_cache_a_symlink(self) -> None:
        docstring = resolve_module.__doc__ or ""
        assert "symlink" not in docstring.lower(), (
            "module docstring still describes the env cache as a symlink; "
            "since #1316 the in-worktree cache is a regular file copy"
        )
        assert "env cache" in docstring, (
            "module docstring must still mention the env cache as the first resolution anchor"
        )

    def test_find_env_cache_docstring_describes_copy_not_symlink(self) -> None:
        docstring = _find_env_cache.__doc__ or ""
        assert "worktree symlinks" not in docstring, (
            "_find_env_cache docstring still claims the in-worktree cache "
            "is a symlink; since #1316 it is a regular file copy"
        )

    def test_resolve_worktree_step1_comment_does_not_mention_symlink(self) -> None:
        source = self._resolve_source()
        # Locate the step-1 walk-up block inside resolve_worktree.
        marker = "# 1. Walk up from CWD to find the env cache"
        assert marker in source, "step-1 walk-up comment moved or was removed"
        start = source.index(marker)
        block = source[start : start + 200]
        assert "symlink" not in block.lower(), (
            f"step-1 inline comment in resolve_worktree still mentions "
            f"'symlink'; since #1316 the in-worktree env cache is a "
            f"regular file copy. Offending block:\n{block}"
        )
