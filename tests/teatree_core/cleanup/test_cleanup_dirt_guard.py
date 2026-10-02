"""The dirty-worktree KEEP guard at its own seam — what it keeps, and what it says.

Both a modified file and an unreadable working tree keep the worktree; only one of
them is evidence. These pin that the guard's sentence says which it is, so an
inconclusive probe is never read as proven uncommitted work.

Real git under ``tmp_path``.
"""

from pathlib import Path

import pytest
from django.test import TestCase

from teatree.core.cleanup.cleanup import _EffectiveTarget
from teatree.core.cleanup.cleanup_dirt_guard import guard_or_warn_dirty_worktree, kept_worktree_message
from teatree.core.cleanup.working_tree_dirt import WorkingTreeDirt
from teatree.core.models import Ticket, Worktree
from tests.teatree_core.cleanup._shared import _run_git, corrupt_index


class DirtGuardTest(TestCase):
    @pytest.fixture(autouse=True)
    def _tmp(self, tmp_path: Path) -> None:
        self.tmp = tmp_path

    def _worktree(self) -> tuple[Path, Worktree, _EffectiveTarget]:
        repo = self.tmp / "repo"
        repo.mkdir()
        _run_git("init", "-q", "-b", "main", cwd=repo)
        _run_git("config", "user.email", "t@t", cwd=repo)
        _run_git("config", "user.name", "t", cwd=repo)
        (repo / "tracked.py").write_text("x = 1\n", encoding="utf-8")
        _run_git("add", "-A", cwd=repo)
        _run_git("commit", "-q", "-m", "initial", cwd=repo)

        wt_dir = self.tmp / "wt"
        _run_git("worktree", "add", "-q", "-b", "feat", str(wt_dir), cwd=repo)

        ticket = Ticket.objects.create(issue_url="https://example.invalid/org/repo/issues/1")
        row = Worktree.objects.create(
            overlay="", ticket=ticket, repo_path="org/repo", branch="feat", extra={"worktree_path": str(wt_dir)}
        )
        target = _EffectiveTarget(ref="HEAD", probe_repo=str(wt_dir), branch_to_delete="feat", label="feat")
        return wt_dir, row, target

    def test_a_clean_worktree_is_not_guarded(self) -> None:
        wt_dir, row, target = self._worktree()

        guard_or_warn_dirty_worktree(row, str(wt_dir), target, keep_if_dirty=True, force=False)

    def test_a_modified_file_refuses_as_uncommitted_changes(self) -> None:
        wt_dir, row, target = self._worktree()
        (wt_dir / "tracked.py").write_text("x = 2\n", encoding="utf-8")

        with pytest.raises(RuntimeError, match="uncommitted changes"):
            guard_or_warn_dirty_worktree(row, str(wt_dir), target, keep_if_dirty=True, force=False)

    def test_an_unreadable_working_tree_keeps_without_claiming_dirt(self) -> None:
        wt_dir, row, target = self._worktree()
        corrupt_index(wt_dir)

        with pytest.raises(RuntimeError) as refusal:
            guard_or_warn_dirty_worktree(row, str(wt_dir), target, keep_if_dirty=True, force=False)

        assert "uncommitted changes" not in str(refusal.value)
        assert "could not answer" in str(refusal.value)

    def test_force_overrides_both_outcomes(self) -> None:
        wt_dir, row, target = self._worktree()
        corrupt_index(wt_dir)

        guard_or_warn_dirty_worktree(row, str(wt_dir), target, keep_if_dirty=True, force=True)

    def test_the_message_names_the_worktree_in_both_shapes(self) -> None:
        _wt_dir, row, _target = self._worktree()
        proven = WorkingTreeDirt(reasons=("1 uncommitted change(s) not on any remote: a.py",), proven=True)
        unproven = WorkingTreeDirt(reasons=("could not read working-tree status (boom) — keeping",), proven=False)

        assert "refused cleanup" in kept_worktree_message(row, "/wt", proven)
        assert "kept, unverified" in kept_worktree_message(row, "/wt", unproven)
