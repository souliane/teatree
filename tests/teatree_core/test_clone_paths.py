# test-path: cross-cutting
"""Source-clone resolution under ``find_clone_path`` and ``resolve_clone_path``.

Real git clones under ``tmp_path``; the only mocked thing is nothing. The
missing-workspace-dir case matters because the per-overlay ``workspace_dir``
default (``~/workspace/t3-workspaces/<overlay>/``) may not exist yet on a fresh
setup — clone resolution must degrade to "no clone" rather than crash.

A recorded ``extra['clone_path']`` is likewise a claim, not a fact: eleven of
twelve rows on a real host pointed at a directory the deploy had since moved away
from. Handed back verbatim, every git probe below it fails, and the redundancy
layers render that failure as an empty unique-commit list — the shape the
judgment skill reads as safe to delete.
"""

from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.models import Ticket, Worktree
from teatree.core.worktree.branch_classification import effective_default_target, reset_single_branch_cache
from teatree.core.worktree.clone_paths import (
    clone_path_from_checkout,
    find_clone_path,
    git_common_clone_dir,
    repair_stale_clone_path,
    resolve_clone_path,
    stored_clone_path,
)
from tests.teatree_core.cleanup._shared import _run_git


def _init_clone(path: Path) -> Path:
    path.mkdir(parents=True)
    _run_git("init", "-q", "-b", "main", cwd=path)
    _run_git("config", "user.email", "t@t", cwd=path)
    _run_git("config", "user.name", "t", cwd=path)
    _run_git("commit", "--allow-empty", "-q", "-m", "init", cwd=path)
    return path


def test_returns_none_when_workspace_dir_does_not_exist(tmp_path: Path) -> None:
    missing = tmp_path / "workspace" / "t3-workspaces" / "myoverlay"
    assert find_clone_path(missing, "myrepo") is None


def test_resolves_literal_clone(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _init_clone(workspace / "myrepo")
    assert find_clone_path(workspace, "myrepo") == workspace / "myrepo"


def test_resolves_namespaced_clone_by_basename(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _init_clone(workspace / "souliane" / "teatree")
    assert find_clone_path(workspace, "teatree") == workspace / "souliane" / "teatree"


def test_returns_none_when_no_clone_matches(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    assert find_clone_path(workspace, "absent") is None


class _StoredClonePathCase(TestCase):
    """A real ``<workspace>/myrepo`` clone plus a row whose stored path is set per test."""

    @pytest.fixture(autouse=True)
    def _tmp_workspace(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.workspace = tmp_path / "workspace"
        self.clone = _init_clone(self.workspace / "myrepo")

    def _row(self, stored: str) -> Worktree:
        ticket = Ticket.objects.create(issue_url=f"https://example.com/issues/{Ticket.objects.count() + 3699}")
        extra = {"worktree_path": str(self.tmp_path / "wt")}
        if stored:
            extra["clone_path"] = stored
        return Worktree.objects.create(overlay="test", ticket=ticket, repo_path="myrepo", branch="feat-x", extra=extra)


class TestStoredClonePath(_StoredClonePathCase):
    def test_only_a_confirmed_checkout_is_handed_back(self) -> None:
        assert stored_clone_path(self._row(str(self.clone))) == self.clone
        assert stored_clone_path(self._row(str(self.tmp_path / "gone"))) is None
        assert stored_clone_path(self._row("")) is None


class TestResolveClonePath(_StoredClonePathCase):
    def test_stale_stored_path_falls_through_to_the_scan(self) -> None:
        row = self._row(str(self.tmp_path / "moved-away" / "souliane" / "myrepo"))

        assert resolve_clone_path(self.workspace, row) == self.clone

    def test_a_directory_that_is_not_a_checkout_is_not_trusted(self) -> None:
        not_a_checkout = self.tmp_path / "plain-dir"
        not_a_checkout.mkdir()

        assert resolve_clone_path(self.workspace, self._row(str(not_a_checkout))) == self.clone

    def test_a_live_stored_path_outside_the_workspace_is_preferred(self) -> None:
        elsewhere = _init_clone(self.tmp_path / "elsewhere" / "myrepo")

        assert resolve_clone_path(self.workspace, self._row(str(elsewhere))) == elsewhere

    def test_no_clone_anywhere_resolves_to_none(self) -> None:
        row = self._row(str(self.tmp_path / "gone"))
        row.repo_path = "ghostrepo"

        assert resolve_clone_path(self.workspace, row) is None

    def test_a_row_without_a_stored_path_still_scans(self) -> None:
        assert resolve_clone_path(self.workspace, self._row("")) == self.clone


class TestRepairStaleClonePath(_StoredClonePathCase):
    def test_a_stale_stored_path_is_rewritten_to_the_real_clone(self) -> None:
        row = self._row(str(self.tmp_path / "moved-away" / "myrepo"))

        assert repair_stale_clone_path(self.workspace, row) == self.clone
        row.refresh_from_db()
        assert row.extra["clone_path"] == str(self.clone)

    def test_a_live_stored_path_is_left_alone(self) -> None:
        elsewhere = _init_clone(self.tmp_path / "elsewhere" / "myrepo")
        row = self._row(str(elsewhere))

        assert repair_stale_clone_path(self.workspace, row) is None
        row.refresh_from_db()
        assert row.extra["clone_path"] == str(elsewhere)

    def test_a_stale_path_with_no_replacement_keeps_the_breadcrumb(self) -> None:
        stale = str(self.tmp_path / "gone")
        row = self._row(stale)
        row.repo_path = "ghostrepo"
        row.save(update_fields=["repo_path"])

        assert repair_stale_clone_path(self.workspace, row) is None
        row.refresh_from_db()
        assert row.extra["clone_path"] == stale, "blanking the row would erase the only record of the clone"


class TestGitIsAskedBeforeGuessingByName(_StoredClonePathCase):
    """A worktree git created is resolvable even when NO name matches it.

    Both name-based tiers miss a worktree made by a bare ``git worktree add``: it
    carries no recorded ``clone_path``, and its directory basename is whatever the
    operator typed rather than the repo. The resolution then landed on
    ``workspace / <basename>`` — a path that does not exist — so teardown reported
    "source repo missing" and removed nothing while still printing a cleaned line,
    and the redundancy probes reported the branch unverifiable. Git knows which
    clone a worktree belongs to; these pin that it is asked.
    """

    def _adhoc_worktree(self, name: str, branch: str) -> Path:
        wt = self.tmp_path / name
        _run_git("worktree", "add", "-q", "-b", branch, str(wt), cwd=self.clone)
        return wt

    def _row_for(self, wt: Path, *, repo_path: str) -> Worktree:
        ticket = Ticket.objects.create(issue_url=f"https://example.com/issues/{Ticket.objects.count() + 4801}")
        return Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path=repo_path,
            branch="feat-y",
            extra={"worktree_path": str(wt)},
        )

    def test_git_common_clone_dir_names_the_clone(self) -> None:
        wt = self._adhoc_worktree("nothing-like-the-repo", "feat-y")

        assert git_common_clone_dir(str(wt)) == self.clone

    def test_a_non_worktree_path_resolves_to_none(self) -> None:
        assert git_common_clone_dir(str(self.tmp_path / "not-a-worktree")) is None

    def test_a_blank_path_never_resolves_to_the_calling_cwd(self) -> None:
        """``Path("")`` is ``.``, so a blank path would answer with the CLI's own repo.

        ``parametrize`` does not reach a ``TestCase`` method, so the two blank
        spellings are asserted directly rather than as separate cases.
        """
        assert git_common_clone_dir("") is None
        assert git_common_clone_dir("   ") is None

    def test_clone_path_from_checkout_requires_a_live_checkout(self) -> None:
        wt = self._adhoc_worktree("some-dir", "feat-y")

        assert clone_path_from_checkout(str(wt)) == self.clone

    def test_a_worktree_whose_basename_matches_no_repo_still_resolves(self) -> None:
        """The exact shape that made teardown a no-op: basename ≠ repo, no stored path."""
        wt = self._adhoc_worktree("tach-boundary", "feat-y")
        row = self._row_for(wt, repo_path="tach-boundary")

        assert resolve_clone_path(self.workspace, row) == self.clone

    def test_a_live_stored_path_still_wins_over_the_git_probe(self) -> None:
        wt = self._adhoc_worktree("some-dir", "feat-y")
        row = self._row_for(wt, repo_path="tach-boundary")
        other = _init_clone(self.tmp_path / "elsewhere")
        row.extra = {**(row.extra or {}), "clone_path": str(other)}
        row.save(update_fields=["extra"])

        assert resolve_clone_path(self.workspace, row) == other

    def test_a_row_whose_worktree_is_gone_falls_through_to_the_name_scan(self) -> None:
        row = self._row_for(self.tmp_path / "vanished", repo_path="myrepo")

        assert resolve_clone_path(self.workspace, row) == self.clone


class TestSingleBranchRepoRedirectsTheRedundancyTarget(TestCase):
    """A bootstrap repo's real target is its pinned branch, not the forge default.

    On a fork bootstrap the forge default is still the empty initial commit while
    every change lands on one long-lived branch behind one open PR. Measured
    against the default, every branch reads as thousands of commits ahead, so the
    reaper keeps ALL of them — each for a reason that is an artefact of the wrong
    base. That is what left 31 worktrees standing over 37 branches.
    """

    @pytest.fixture(autouse=True)
    def _tmp(self, tmp_path: Path) -> None:
        self.repo = _init_clone(tmp_path / "widget-core")
        _run_git("remote", "add", "origin", "git@example.com:org/group/widget-core.git", cwd=self.repo)

    def _target(self, declared: list[str]) -> str:
        class _Settings:
            single_branch_repos = declared

        reset_single_branch_cache()
        with patch("teatree.config.get_effective_settings", return_value=_Settings()):
            return effective_default_target(str(self.repo))

    def test_a_declared_repo_targets_its_pinned_branch(self) -> None:
        assert self._target(["group/widget-core=chore/fork-bootstrap"]) == "origin/chore/fork-bootstrap"

    def test_an_undeclared_repo_keeps_the_forge_default(self) -> None:
        assert self._target(["group/other=chore/x"]).endswith("/main")

    def test_no_declaration_at_all_keeps_the_forge_default(self) -> None:
        assert self._target([]).endswith("/main")

    def test_an_unreadable_config_never_blocks_target_resolution(self) -> None:
        reset_single_branch_cache()
        with patch("teatree.config.get_effective_settings", side_effect=RuntimeError("no db")):
            assert effective_default_target(str(self.repo)).endswith("/main")
        reset_single_branch_cache()

    def test_the_declaration_is_read_once_not_per_branch(self) -> None:
        """The reaper calls this a few hundred times a run; an uncached read timed it out."""

        class _Settings:
            single_branch_repos: ClassVar[list[str]] = ["group/widget-core=chore/fork-bootstrap"]

        reset_single_branch_cache()
        with patch("teatree.config.get_effective_settings", return_value=_Settings()) as read:
            for _ in range(5):
                effective_default_target(str(self.repo))

        assert read.call_count == 1
        reset_single_branch_cache()
