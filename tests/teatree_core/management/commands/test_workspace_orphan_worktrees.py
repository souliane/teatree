"""Real-git integration for orphaned RAW worktree reaping (#2361).

A sub-agent's bare ``git worktree add`` leaves a worktree with NO teatree
``Worktree`` row. ``clean-all``'s row-driven reaper never touches it, so it
accumulates (a real host reached 183). These tests drive
:func:`reap_orphan_raw_worktrees` against a real ``git worktree`` under
``tmp_path`` and prove BOTH directions: an orphan whose work is on a remote (or
detached with nothing unique) is reaped; an orphan with unpushed unique work (or
uncommitted changes) is KEPT — the #706 data-loss guard, never destroyed and
never snapshot. Salvage is a separate explicit action (push the branch to a PR).
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.management.commands._workspace.orphan_worktrees import (
    _db_tracked_paths,
    _raw_worktree_paths,
    reap_orphan_raw_worktrees,
)
from teatree.core.models import Ticket, Worktree
from tests.teatree_core.cleanup._shared import _GIT, _clean_env, _run_git


class _OrphanWorktreeFixture(TestCase):
    """A main clone + a bare ``origin`` it can push to, under ``tmp_path``."""

    @pytest.fixture(autouse=True)
    def _tmp_workspace(self, tmp_path: Path) -> None:
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir()
        self.origin = tmp_path / "origin.git"
        self.origin.mkdir()
        _run_git("init", "-q", "--bare", "-b", "main", cwd=self.origin)
        self.repo_main = self.workspace / "myrepo"
        self.repo_main.mkdir()
        _run_git("init", "-q", "-b", "main", cwd=self.repo_main)
        _run_git("config", "user.email", "t@t", cwd=self.repo_main)
        _run_git("config", "user.name", "t", cwd=self.repo_main)
        _run_git("remote", "add", "origin", str(self.origin), cwd=self.repo_main)
        (self.repo_main / "README").write_text("x")
        _run_git("add", "-A", cwd=self.repo_main)
        _run_git("commit", "-q", "-m", "initial", cwd=self.repo_main)
        _run_git("push", "-q", "-u", "origin", "main", cwd=self.repo_main)

    def _add_orphan(self, branch: str, *, files: dict[str, str] | None = None, detach: bool = False) -> Path:
        """Create a raw ``git worktree`` (no DB row) on ``branch`` with optional commits."""
        wt_path = self.workspace / branch / "myrepo"
        if detach:
            _run_git("worktree", "add", "-q", "--detach", str(wt_path), "HEAD", cwd=self.repo_main)
        else:
            _run_git("worktree", "add", "-q", "-b", branch, str(wt_path), cwd=self.repo_main)
        for name, content in (files or {}).items():
            (wt_path / name).write_text(content)
            _run_git("add", "-A", cwd=wt_path)
            _run_git("commit", "-q", "-m", f"add {name}", cwd=wt_path)
        return wt_path

    def _registered_paths(self) -> str:
        return subprocess.run(
            [_GIT, "-C", str(self.repo_main), "worktree", "list"],
            check=True,
            capture_output=True,
            text=True,
            env=_clean_env(),
        ).stdout

    def _reap(self, *, dry_run: bool = False) -> list[str]:
        # Force cwd-based clone discovery onto the tmp main clone.
        with (
            patch(
                "teatree.core.management.commands._workspace.orphan_worktrees.is_clean_ignored",
                return_value=False,
            ),
            patch(
                "teatree.core.management.commands._workspace.orphan_worktrees.Path.cwd",
                return_value=self.repo_main,
            ),
        ):
            return reap_orphan_raw_worktrees(self.workspace, dry_run=dry_run)


class TestRawWorktreeDiscovery(_OrphanWorktreeFixture):
    def test_lists_linked_worktrees_excluding_the_main_checkout(self) -> None:
        wt_path = self._add_orphan("feat-a")
        worktrees = _raw_worktree_paths(str(self.repo_main))
        assert str(self.repo_main) not in {str(Path(p)) for p in worktrees}
        assert str(wt_path) in worktrees
        assert worktrees[str(wt_path)] == "feat-a"

    def test_detached_worktree_records_head(self) -> None:
        wt_path = self._add_orphan("detached-x", detach=True)
        worktrees = _raw_worktree_paths(str(self.repo_main))
        assert worktrees[str(wt_path)] == "HEAD"

    def test_db_tracked_paths_are_absolute(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/1", state=Ticket.State.IN_REVIEW)
        Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="myrepo",
            branch="tracked",
            extra={"worktree_path": str(self.workspace / "tracked" / "myrepo")},
        )
        assert str((self.workspace / "tracked" / "myrepo").resolve()) in _db_tracked_paths()


class TestReapsMergedOrphan(_OrphanWorktreeFixture):
    def test_orphan_whose_work_is_on_remote_is_reaped(self) -> None:
        """A branch fully pushed to origin has no unique work — safe to reap."""
        wt_path = self._add_orphan("synced-feat", files={"f.txt": "hi"})
        _run_git("push", "-q", "-u", "origin", "synced-feat", cwd=wt_path)

        results = self._reap()

        assert not wt_path.exists(), "synced orphan worktree survived"
        assert str(wt_path) not in self._registered_paths()
        assert any("Reaped orphan worktree (work already on remote)" in line for line in results)

    def test_detached_orphan_with_no_unique_commit_is_reaped(self) -> None:
        wt_path = self._add_orphan("detach-clean", detach=True)
        results = self._reap()
        assert not wt_path.exists()
        assert any("Reaped orphan worktree" in line for line in results)

    def test_dry_run_previews_the_reap_without_removing(self) -> None:
        # #3489: a reapable orphan is PREVIEWED, prefixed WOULD, and left on disk.
        wt_path = self._add_orphan("synced-preview", files={"f.txt": "hi"})
        _run_git("push", "-q", "-u", "origin", "synced-preview", cwd=wt_path)

        results = self._reap(dry_run=True)

        assert wt_path.exists(), "dry-run must not remove the orphan worktree"
        assert str(wt_path) in self._registered_paths()
        assert any(line.startswith("WOULD Reap orphan worktree") and "synced-preview" in line for line in results)


class TestKeepsUnpushedOrphan(_OrphanWorktreeFixture):
    def test_unpushed_orphan_is_kept_by_default(self) -> None:
        """The 183-accumulation case: unpushed unique work is KEPT (data-loss guard), never snapshot."""
        wt_path = self._add_orphan("unpushed-feat", files={"new.txt": "secret work"})

        results = self._reap()

        assert wt_path.exists(), "unpushed orphan must NOT be reaped — it carries unique work"
        assert str(wt_path) in self._registered_paths()
        assert any("KEPT orphan" in line and "unpushed-feat" in line for line in results)

    def test_dirty_orphan_is_always_kept(self) -> None:
        """A live worktree with uncommitted changes is never reaped."""
        wt_path = self._add_orphan("dirty-feat")
        (wt_path / "wip.txt").write_text("mid-task edit")  # uncommitted

        results = self._reap()

        assert wt_path.exists(), "dirty orphan must never be reaped"
        assert any("uncommitted changes" in line for line in results)


class TestReapsSquashMergedOrphan(_OrphanWorktreeFixture):
    def test_squash_merged_orphan_with_deleted_remote_branch_is_reaped_under_keep(self) -> None:
        """A single-commit branch squash-merged into main, its remote branch deleted on merge.

        The dominant teatree case. ``--not --remotes`` still reports the commit (the squash
        produced a NEW SHA), but ``is_squash_merged`` (patch-id ``git cherry``) sees the work
        captured upstream — so the orphan is recoverable and REAPED, not kept.
        """
        wt_path = self._add_orphan("squashed-feat", files={"feature.txt": "the feature"})

        # Squash the branch's single commit into main with a new SHA, push, and delete
        # the remote branch — exactly what a forge squash-merge leaves behind.
        _run_git("checkout", "-q", "main", cwd=self.repo_main)
        _run_git("merge", "-q", "--squash", "squashed-feat", cwd=self.repo_main)
        _run_git("commit", "-q", "-m", "squash: the feature (#1)", cwd=self.repo_main)
        _run_git("push", "-q", "origin", "main", cwd=self.repo_main)

        results = self._reap()

        assert not wt_path.exists(), "squash-merged orphan survived despite work being on main"
        assert str(wt_path) not in self._registered_paths()
        assert any("Reaped orphan worktree (work already on remote)" in line for line in results), results


class TestTrackedWorktreeNeverReapedAsOrphan(_OrphanWorktreeFixture):
    def test_db_tracked_worktree_is_excluded_from_orphan_reaping(self) -> None:
        """A worktree WITH a DB row is not an orphan — the row-driven reaper owns it."""
        wt_path = self._add_orphan("tracked-feat", files={"t.txt": "tracked work"})
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/7", state=Ticket.State.IN_REVIEW)
        Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="myrepo",
            branch="tracked-feat",
            extra={"worktree_path": str(wt_path), "clone_path": str(self.repo_main)},
        )

        results = self._reap()

        assert wt_path.exists(), "a DB-tracked worktree must never be reaped by the orphan pass"
        assert not any("tracked-feat" in line for line in results)


class TestStaleRemoteTrackingRefNeverReaped(_OrphanWorktreeFixture):
    """The reaper must not read a stale tracking ref as proof the work is pushed.

    ``commits_absent_from_all_remotes`` is a local graph query over
    ``refs/remotes/*``. When a branch is deleted upstream by anything other than
    this clone — a forge's auto-delete-on-merge, or a sibling clone — the local
    tracking ref survives and still contains the tip, so the probe reports "on a
    remote" for work that exists on no remote at all. That misread reaped
    branches carrying real unmerged work.
    """

    def _delete_upstream_only(self, branch: str) -> None:
        """Delete ``branch`` in the bare origin, leaving this clone's tracking ref stale.

        Deliberately NOT ``git push --delete`` from the clone: that removes the
        local tracking ref too, which self-heals the bug and would make this test
        pass against the unfixed code.
        """
        _run_git("update-ref", "-d", f"refs/heads/{branch}", cwd=self.origin)

    def test_unmerged_work_is_kept_when_its_tracking_ref_is_stale(self) -> None:
        wt_path = self._add_orphan("review-fixes", files={"important.txt": "unmerged work"})
        _run_git("push", "-q", "-u", "origin", "review-fixes", cwd=wt_path)
        self._delete_upstream_only("review-fixes")

        results = self._reap()

        assert wt_path.exists(), "orphan holding unmerged work was reaped on a STALE tracking ref"
        assert str(wt_path) in self._registered_paths()
        assert any("unpushed work not on any remote" in line for line in results), results

    def test_branch_merged_upstream_is_still_reaped(self) -> None:
        """The fix must not turn every deleted-upstream branch into a keeper."""
        wt_path = self._add_orphan("shipped-feat", files={"shipped.txt": "work that landed"})
        _run_git("push", "-q", "-u", "origin", "shipped-feat", cwd=wt_path)
        # Fast-forward main upstream to the branch tip, then delete the branch: the
        # commits stay reachable from origin/main, so the worktree IS redundant.
        _run_git("push", "-q", "origin", "HEAD:main", cwd=wt_path)
        self._delete_upstream_only("shipped-feat")

        results = self._reap()

        assert not wt_path.exists(), "orphan whose work is on origin/main should still be reaped"
        assert any("Reaped orphan worktree (work already on remote)" in line for line in results), results

    def test_unreachable_remote_fails_closed_and_reaps_nothing(self) -> None:
        """An unrefreshable clone keeps every orphan — unknown remote state never authorises deletion."""
        wt_path = self._add_orphan("offline-feat", files={"o.txt": "work"})
        _run_git("push", "-q", "-u", "origin", "offline-feat", cwd=wt_path)
        _run_git("remote", "set-url", "origin", str(self.workspace / "gone.git"), cwd=self.repo_main)

        results = self._reap()

        assert wt_path.exists(), "a clone whose remote refs could not be refreshed must keep its orphans"
        assert any("could not refresh remote refs" in line for line in results), results
