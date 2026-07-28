"""The narrow release of registered rows whose checkout is provably dead.

``clean-all`` can release these rows, but only as one pass inside a sweep that also
prunes branches, drops databases, and reaps docker projects. An operator who needs
ONLY the rows cleared should not have to authorise all of that. These tests pin the
narrowness as a first-class property: the command releases DB rows and provably
leaves the directory, the branch, and every other pass untouched.

Functional throughout: real clones, real ``git worktree add``, real rows.
"""

import os
import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase

from teatree.core.models import Ticket, Worktree
from teatree.core.worktree.dead_row_release import DeadRowDisposition, plan_dead_row_release, release_dead_rows
from teatree.utils import git
from teatree.utils.run import run_checked

_OLD = "2020-01-01T00:00:00 +0000"
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_COMMITTER_DATE": _OLD,
    "GIT_AUTHOR_DATE": _OLD,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def _git(repo: Path, *args: str) -> None:
    hermetic = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    run_checked(["git", "-C", str(repo), *args], env={**hermetic, **_GIT_ENV})


class _DeadRowCase(TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.workspace = self.tmp / "workspace"
        self.workspace.mkdir()
        self.remote = self.workspace / "remote.git"
        _git(self.workspace, "init", "--quiet", "--bare", "--initial-branch=main", str(self.remote))
        self.clone = self.workspace / "backend"
        _git(self.workspace, "clone", "--quiet", str(self.remote), str(self.clone))
        (self.clone / "README.md").write_text("base\n", encoding="utf-8")
        _git(self.clone, "add", "README.md")
        _git(self.clone, "commit", "-q", "-m", "chore: base")
        _git(self.clone, "push", "-q", "origin", "main")

    def _register(self, wt_path: Path, *, branch: str) -> Worktree:
        ticket = Ticket.objects.create(overlay="", issue_url=f"https://example.invalid/org/repo/issues/{branch}")
        return Worktree.objects.create(
            overlay="",
            ticket=ticket,
            repo_path="backend",
            branch=branch,
            extra={"clone_path": str(self.clone), "worktree_path": str(wt_path)},
        )

    def _worktree_on(self, branch: str, *, commit: str | None = None) -> Path:
        wt_path = self.workspace / f"wt-{branch}"
        _git(self.clone, "worktree", "add", "-q", "-b", branch, str(wt_path))
        if commit is not None:
            (wt_path / "work.py").write_text(f"{commit}\n", encoding="utf-8")
            _git(wt_path, "add", "work.py")
            _git(wt_path, "commit", "-q", "-m", commit)
        return wt_path

    def _break_checkout(self, wt_path: Path) -> None:
        admin = self.clone / ".git" / "worktrees" / wt_path.name
        for child in sorted(admin.rglob("*"), reverse=True):
            child.unlink() if child.is_file() else child.rmdir()
        admin.rmdir()

    def _dead_row_with_gone_ref(self, branch: str = "vanished") -> tuple[Worktree, Path]:
        """The production shape: dir exists, is not a repo, and its branch ref is gone."""
        wt_path = self._worktree_on(branch, commit="feat: shipped")
        row = self._register(wt_path, branch=branch)
        self._break_checkout(wt_path)
        _git(self.clone, "branch", "-D", branch)
        return row, wt_path


class ReleasesOnlyTheRowTest(_DeadRowCase):
    def test_a_dead_checkout_row_is_released(self) -> None:
        row, _wt_path = self._dead_row_with_gone_ref()

        release_dead_rows(self.workspace, dry_run=False)

        assert not Worktree.objects.filter(pk=row.pk).exists()

    def test_the_directory_survives_the_release(self) -> None:
        _row, wt_path = self._dead_row_with_gone_ref()
        before = sorted(p.name for p in wt_path.iterdir())

        release_dead_rows(self.workspace, dry_run=False)

        assert wt_path.is_dir(), "the release must never remove the directory"
        assert sorted(p.name for p in wt_path.iterdir()) == before

    def test_a_surviving_branch_is_never_deleted(self) -> None:
        # A row whose branch shipped is releasable, but the release still has no
        # business deleting the ref — that is the branch-prune pass's decision.
        wt_path = self._worktree_on("shipped", commit="feat: shipped work")
        _git(wt_path, "push", "-q", "origin", "shipped")
        row = self._register(wt_path, branch="shipped")
        self._break_checkout(wt_path)

        release_dead_rows(self.workspace, dry_run=False)

        assert not Worktree.objects.filter(pk=row.pk).exists()
        assert git.check(repo=str(self.clone), args=["show-ref", "--verify", "--quiet", "refs/heads/shipped"])

    def test_a_live_checkout_row_is_left_alone(self) -> None:
        wt_path = self._worktree_on("live", commit="feat: in progress")
        row = self._register(wt_path, branch="live")

        release_dead_rows(self.workspace, dry_run=False)

        assert Worktree.objects.filter(pk=row.pk).exists()
        assert wt_path.is_dir()

    def test_a_dead_checkout_holding_unpushed_work_is_kept(self) -> None:
        wt_path = self._worktree_on("unshipped", commit="feat: never pushed")
        row = self._register(wt_path, branch="unshipped")
        self._break_checkout(wt_path)

        lines = release_dead_rows(self.workspace, dry_run=False)

        assert Worktree.objects.filter(pk=row.pk).exists()
        assert any("KEPT" in line and "unshipped" in line for line in lines), lines

    def test_dry_run_is_the_default_shape_and_removes_nothing(self) -> None:
        row, wt_path = self._dead_row_with_gone_ref()

        lines = release_dead_rows(self.workspace, dry_run=True)

        assert Worktree.objects.filter(pk=row.pk).exists()
        assert wt_path.is_dir()
        assert any("WOULD RELEASE" in line for line in lines), lines

    def test_the_dry_run_preview_matches_what_the_live_run_releases(self) -> None:
        # A preview that under-reports a destructive command is worse than no preview.
        releasable, _ = self._dead_row_with_gone_ref("gone")
        kept_path = self._worktree_on("holds", commit="feat: never pushed")
        kept = self._register(kept_path, branch="holds")
        self._break_checkout(kept_path)

        previewed = {v.worktree_pk for v in plan_dead_row_release(self.workspace) if v.releasable}
        release_dead_rows(self.workspace, dry_run=False)
        actually_gone = {pk for pk in (releasable.pk, kept.pk) if not Worktree.objects.filter(pk=pk).exists()}

        assert previewed == actually_gone == {releasable.pk}


class FailClosedTest(_DeadRowCase):
    def test_an_unrefreshable_clone_keeps_the_row(self) -> None:
        wt_path = self._worktree_on("shipped", commit="feat: shipped work")
        _git(wt_path, "push", "-q", "origin", "shipped")
        row = self._register(wt_path, branch="shipped")
        self._break_checkout(wt_path)

        with mock.patch.object(git, "fetch_all_prune", return_value=False):
            release_dead_rows(self.workspace, dry_run=False)

        assert Worktree.objects.filter(pk=row.pk).exists()

    def test_one_clone_is_refreshed_once_for_many_rows(self) -> None:
        # Ten rows on one clone must cost one fetch, not ten.
        for n in range(3):
            wt_path = self._worktree_on(f"shipped-{n}", commit=f"feat: work {n}")
            _git(wt_path, "push", "-q", "origin", f"shipped-{n}")
            self._register(wt_path, branch=f"shipped-{n}")
            self._break_checkout(wt_path)

        with mock.patch.object(git, "fetch_all_prune", return_value=True) as fetch:
            release_dead_rows(self.workspace, dry_run=False)

        assert fetch.call_count == 1, f"expected one memoised refresh, got {fetch.call_count}"


class DispositionReportingTest(_DeadRowCase):
    def test_every_examined_row_carries_a_disposition_and_a_reason(self) -> None:
        self._dead_row_with_gone_ref("gone")
        kept_path = self._worktree_on("holds", commit="feat: never pushed")
        self._register(kept_path, branch="holds")
        self._break_checkout(kept_path)

        verdicts = {v.branch: v for v in plan_dead_row_release(self.workspace)}

        assert verdicts["gone"].disposition is DeadRowDisposition.RELEASABLE
        assert verdicts["holds"].disposition is DeadRowDisposition.HOLDS_WORK
        assert all(v.reason for v in verdicts.values())

    def test_a_live_checkout_is_not_reported_at_all(self) -> None:
        wt_path = self._worktree_on("live", commit="feat: in progress")
        self._register(wt_path, branch="live")

        assert plan_dead_row_release(self.workspace) == []
