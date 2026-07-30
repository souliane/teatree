"""``workspace doctor --fix`` tears down dir-gone ``Worktree`` rows for real.

A ``MissingWorktreeDir`` finding used to blank the row's ``extra`` and leave the
row itself behind forever — ghost rows accumulated (a real host reached 33 of
53), each pinning its ``db_name`` and thereby shielding the leaked per-worktree
database from the orphan-DB reaper. These tests drive :func:`_fix_drift`
against a real main clone under ``tmp_path`` and prove all three dispositions:
a pure ghost (no dir, no branch ref) is fully torn down; a dir-gone row whose
surviving branch ref carries unpushed commits is KEPT (#706); a dir-gone row
whose branch is safely on the remote is torn down through the ordinary guards.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase

from teatree.core.management.commands._workspace.cleanup import _fix_drift
from teatree.core.models import Ticket, Worktree
from teatree.core.worktree.reconcile import Drift, EnvCacheDrift, MissingEnvCache, MissingWorktreeDir
from tests.teatree_core.cleanup._shared import _run_git


class _DirGoneRowFixture(TestCase):
    """A real main clone (+ file ``origin``) and a Worktree row whose dir is gone."""

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

    def _make_row(self, branch: str, *, db_name: str = "") -> Worktree:
        ticket = Ticket.objects.create(
            issue_url="https://gitlab.com/org/myrepo/-/issues/7",
            state=Ticket.State.MERGED,
        )
        gone = self.workspace / branch / "myrepo"
        return Worktree.objects.create(
            ticket=ticket,
            repo_path="myrepo",
            branch=branch,
            db_name=db_name,
            extra={"worktree_path": str(gone)},
        )

    def _fix(self, row: Worktree) -> tuple[list[str], MagicMock]:
        drift = Drift(
            ticket_pk=row.ticket.pk,
            missing_worktree_dirs=[MissingWorktreeDir(worktree_pk=row.pk, path=Path(row.worktree_path))],
        )
        with (
            patch("teatree.core.cleanup.cleanup.clone_root", return_value=self.workspace),
            patch(
                "teatree.core.management.commands._workspace.cleanup.clone_root",
                return_value=self.workspace,
            ),
            patch("teatree.core.runners.worktree_start.docker_compose_down"),
            patch("teatree.core.cleanup.cleanup.drop_db") as drop,
            patch("teatree.core.cleanup.cleanup._ref_captured_by_merge", return_value=False),
        ):
            return _fix_drift(drift), drop


class TestFixDriftDirGoneRows(_DirGoneRowFixture):
    def test_pure_ghost_row_is_fully_torn_down(self) -> None:
        # Branch was never created (or is long deleted): no dir, no ref —
        # nothing on disk to lose. The row must be DELETED, not left behind
        # with a blanked ``extra``.
        row = self._make_row("ghost-branch", db_name="wt_ghost_7")

        fixes, drop = self._fix(row)

        assert not Worktree.objects.filter(pk=row.pk).exists()
        assert any("tore down ghost" in line for line in fixes)
        # The teardown releases the row's DB instead of pinning it forever.
        drop.assert_called_once()
        assert drop.call_args.args[0] == "wt_ghost_7"

    def test_dir_gone_row_with_unpushed_branch_ref_is_kept(self) -> None:
        # The branch ref survives in the main clone with a commit on NO remote:
        # the #706 data-loss guard must keep the row (and the branch).
        _run_git("checkout", "-q", "-b", "wip-unpushed", cwd=self.repo_main)
        (self.repo_main / "work.txt").write_text("unpushed")
        _run_git("add", "-A", cwd=self.repo_main)
        _run_git("commit", "-q", "-m", "unpushed work", cwd=self.repo_main)
        _run_git("checkout", "-q", "main", cwd=self.repo_main)
        row = self._make_row("wip-unpushed")

        fixes, _ = self._fix(row)

        assert Worktree.objects.filter(pk=row.pk).exists()
        assert any(line.startswith("kept wt#") for line in fixes)

    def test_dir_gone_row_with_pushed_branch_is_torn_down(self) -> None:
        # The branch is fully on the remote: the ordinary guards pass, the row
        # is deleted, and the now-redundant local branch ref is removed.
        _run_git("checkout", "-q", "-b", "shipped", cwd=self.repo_main)
        _run_git("checkout", "-q", "main", cwd=self.repo_main)
        _run_git("push", "-q", "origin", "shipped", cwd=self.repo_main)
        row = self._make_row("shipped")

        fixes, _ = self._fix(row)

        assert not Worktree.objects.filter(pk=row.pk).exists()
        assert any(line.startswith("tore down wt#") for line in fixes)


class TestFixDriftSkipsVanishedRows(TestCase):
    """Each drift finding whose ``Worktree`` row is already gone is skipped, never crashes."""

    def test_none_rows_in_every_category_are_skipped(self) -> None:
        gone = 10_000_000  # a pk no Worktree row has
        drift = Drift(
            ticket_pk=gone,
            missing_worktree_dirs=[MissingWorktreeDir(worktree_pk=gone, path=Path("/gone"))],
            missing_env_caches=[MissingEnvCache(worktree_pk=gone, cache_path=Path("/gone/.cache"))],
            env_cache_drifts=[EnvCacheDrift(worktree_pk=gone, cache_path=Path("/gone/.cache"))],
        )

        # every finding references a vanished row → all three loops `continue`
        assert _fix_drift(drift) == []
