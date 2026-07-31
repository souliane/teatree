"""Unresolvable worktree dirs are REPORTED, never removed (#3912, #3853).

A dir whose ``.git`` pointer does not resolve here used to be wiped on the
reasoning that a broken checkout holds nothing recoverable. A checkout created
in another execution context presents exactly that evidence while being
perfectly healthy, so the pass now reports and deletes nothing — UNKNOWN never
authorises a deletion.

Real dirs under ``tmp_path`` and the real ``git rev-parse`` probe throughout.
"""

import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase

from teatree.core.management.commands._workspace.broken_worktrees import report_unresolvable_worktree_dirs
from teatree.utils import git


def _real_checkout(path: Path) -> Path:
    path.mkdir(parents=True)
    git.run(repo=str(path), args=["init", "--quiet", "--initial-branch=main"])
    return path


def _unresolvable_checkout(path: Path) -> Path:
    """A checkout naming an admin dir this venue cannot reach — live elsewhere, or dead."""
    path.mkdir(parents=True)
    (path / ".git").write_text("gitdir: /nonexistent/other-context/.git/worktrees/gone\n", encoding="utf-8")
    (path / "work.py").write_text("x", encoding="utf-8")
    return path


class _TmpPathTestCase(TestCase):
    """A real on-disk scratch dir per test, torn down even when setUp fails."""

    def setUp(self) -> None:
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.tmp_path = Path(tmp_dir.name)


class TestUnresolvableWorktreeReport(_TmpPathTestCase):
    def test_an_unresolvable_dir_is_named_and_survives(self) -> None:
        candidate = _unresolvable_checkout(self.tmp_path / "roots" / "statusline-refresh")

        outcomes = report_unresolvable_worktree_dirs(self.tmp_path / "roots")

        assert candidate.is_dir(), "an UNKNOWN checkout must never be removed"
        assert (candidate / "work.py").is_file()
        assert any("statusline-refresh" in line for line in outcomes)
        assert any("never removed" in line for line in outcomes)

    def test_the_report_names_the_admin_dir_this_venue_cannot_reach(self) -> None:
        _unresolvable_checkout(self.tmp_path / "roots" / "elsewhere")

        outcomes = report_unresolvable_worktree_dirs(self.tmp_path / "roots")

        assert any("does not exist in this execution context" in line for line in outcomes)

    def test_a_healthy_checkout_is_never_reported(self) -> None:
        healthy = _real_checkout(self.tmp_path / "roots" / "live-work")

        outcomes = report_unresolvable_worktree_dirs(self.tmp_path / "roots")

        assert healthy.is_dir()
        assert outcomes == []

    def test_a_dir_with_no_git_entry_is_not_a_candidate(self) -> None:
        env_dir = self.tmp_path / "roots" / "a1b2c3d4e5f6"
        env_dir.mkdir(parents=True)
        (env_dir / "db.sqlite3").write_text("", encoding="utf-8")

        outcomes = report_unresolvable_worktree_dirs(self.tmp_path / "roots")

        assert env_dir.is_dir(), "an auto-isolated env dir is another reaper's business"
        assert outcomes == []

    def test_a_dir_git_could_not_classify_is_reported_as_unknown(self) -> None:
        candidate = _unresolvable_checkout(self.tmp_path / "roots" / "unanswerable")
        refusal = mock.Mock(returncode=128, stdout="", stderr="fatal: detected dubious ownership in repository")

        with mock.patch("teatree.core.worktree.worktree_roots.run_allowed_to_fail", return_value=refusal):
            outcomes = report_unresolvable_worktree_dirs(self.tmp_path / "roots")

        assert candidate.is_dir()
        assert any("UNKNOWN worktree dir" in line for line in outcomes)

    def test_several_roots_are_reported_in_one_pass(self) -> None:
        _unresolvable_checkout(self.tmp_path / "canonical" / "one")
        _unresolvable_checkout(self.tmp_path / "alternate" / "two")

        outcomes = report_unresolvable_worktree_dirs(self.tmp_path / "canonical", self.tmp_path / "alternate")

        assert any("'one'" in line for line in outcomes)
        assert any("'two'" in line for line in outcomes)

    def test_the_same_candidate_is_visited_once_across_repeated_roots(self) -> None:
        _unresolvable_checkout(self.tmp_path / "roots" / "dupe")

        outcomes = report_unresolvable_worktree_dirs(self.tmp_path / "roots", self.tmp_path / "roots")

        assert len([line for line in outcomes if "dupe" in line]) == 1

    def test_a_root_that_does_not_exist_yields_nothing(self) -> None:
        assert report_unresolvable_worktree_dirs(self.tmp_path / "no-such-root") == []
