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

import pytest
from django.test import TestCase

from teatree.core.models import Ticket, Worktree
from teatree.core.worktree.clone_paths import (
    find_clone_path,
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
