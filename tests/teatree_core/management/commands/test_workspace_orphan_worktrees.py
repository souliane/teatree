"""Real-git integration for orphaned RAW worktree reaping (#2361).

A sub-agent's bare ``git worktree add`` leaves a worktree with NO teatree
``Worktree`` row. ``clean-all``'s row-driven reaper never touches it, so it
accumulates (a real host reached 183). These tests drive
:func:`reap_orphan_raw_worktrees` against a real ``git worktree`` under
``tmp_path`` and prove BOTH directions: an orphan whose work is on a remote (or
detached with nothing unique) is reaped; an orphan with unpushed unique work is
KEPT under the default policy, SNAPSHOTTED-then-reaped under ``snapshot``, and
the #706 guard refuses to reap unique work when no snapshot materialises.
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.management.commands._workspace_orphan_worktrees import (
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

    def _reap(self, *, reap_unsynced: str = "keep") -> list[str]:
        # Force cwd-based clone discovery onto the tmp main clone.
        with (
            patch(
                "teatree.core.management.commands._workspace_orphan_worktrees.is_clean_ignored",
                return_value=False,
            ),
            patch(
                "teatree.core.management.commands._workspace_orphan_worktrees.Path.cwd",
                return_value=self.repo_main,
            ),
        ):
            return reap_orphan_raw_worktrees(self.workspace, reap_unsynced=reap_unsynced)


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


class TestKeepsUnpushedOrphan(_OrphanWorktreeFixture):
    def test_unpushed_orphan_is_kept_under_default_keep_policy(self) -> None:
        """The 183-accumulation case: unpushed unique work is KEPT by default (data-loss guard)."""
        wt_path = self._add_orphan("unpushed-feat", files={"new.txt": "secret work"})

        results = self._reap(reap_unsynced="keep")

        assert wt_path.exists(), "unpushed orphan must NOT be reaped under keep policy"
        assert str(wt_path) in self._registered_paths()
        assert any("KEPT orphan" in line and "unpushed-feat" in line for line in results)

    def test_dirty_orphan_is_always_kept(self) -> None:
        """A live worktree with uncommitted changes is never reaped, even under snapshot policy."""
        wt_path = self._add_orphan("dirty-feat")
        (wt_path / "wip.txt").write_text("mid-task edit")  # uncommitted

        results = self._reap(reap_unsynced="snapshot")

        assert wt_path.exists(), "dirty orphan must never be reaped"
        assert any("uncommitted changes" in line for line in results)


class TestSnapshotThenReap(_OrphanWorktreeFixture):
    def test_unpushed_orphan_is_snapshotted_then_reaped_and_is_recoverable(self) -> None:
        """``--reap-unsynced=snapshot``: write a recovery artifact, THEN reap; commits recoverable."""
        wt_path = self._add_orphan("snapshot-feat", files={"keep.txt": "important work"})
        unique_sha = subprocess.run(
            [_GIT, "-C", str(wt_path), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            env=_clean_env(),
        ).stdout.strip()

        results = self._reap(reap_unsynced="snapshot")

        assert not wt_path.exists(), "snapshot-policy orphan should be reaped"
        assert str(wt_path) not in self._registered_paths()
        reaped = [line for line in results if "Reaped orphan worktree (snapshot at" in line]
        assert reaped, f"expected a snapshot-then-reap line, got {results}"

        snapshot_dir = Path(reaped[0].split("snapshot at ", 1)[1].split(")", 1)[0])
        bundle = snapshot_dir / "branch.bundle"
        assert bundle.is_file(), "recovery bundle was not written"

        # The reaped commit must be present in the bundle and restorable from it.
        heads = subprocess.run(
            [_GIT, "bundle", "list-heads", str(bundle)],
            check=True,
            capture_output=True,
            text=True,
            env=_clean_env(),
        ).stdout
        assert unique_sha in heads, "the unpushed commit is NOT recorded in the recovery bundle"

        restored = snapshot_dir / "restored"
        subprocess.run(
            [_GIT, "clone", "-q", str(bundle), str(restored)],
            check=True,
            capture_output=True,
            env=_clean_env(),
        )
        log = subprocess.run(
            [_GIT, "-C", str(restored), "log", "--format=%H", "origin/snapshot-feat"],
            check=True,
            capture_output=True,
            text=True,
            env=_clean_env(),
        ).stdout
        assert unique_sha in log, "the unpushed commit is NOT recoverable from the cloned snapshot"

    def test_706_guard_refuses_reap_when_snapshot_does_not_materialise(self) -> None:
        """#706: unique work is never reaped WITHOUT a successful snapshot — bundle failure keeps it."""
        wt_path = self._add_orphan("guard-feat", files={"x.txt": "unique"})

        with patch(
            "teatree.core.management.commands._workspace_orphan_worktrees.capture_worktree_snapshot",
            return_value=None,
        ):
            results = self._reap(reap_unsynced="snapshot")

        assert wt_path.exists(), "orphan reaped despite the snapshot not materialising (data loss!)"
        assert str(wt_path) in self._registered_paths()
        assert any("snapshot did not materialise" in line for line in results)


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

        results = self._reap(reap_unsynced="keep")

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

        results = self._reap(reap_unsynced="snapshot")

        assert wt_path.exists(), "a DB-tracked worktree must never be reaped by the orphan pass"
        assert not any("tracked-feat" in line for line in results)
