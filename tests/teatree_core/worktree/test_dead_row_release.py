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
from django.utils import timezone

from teatree.core.models import ConfigSetting, Session, Ticket, Worktree
from teatree.core.worktree.dead_row_release import (
    DeadRowDisposition,
    DeadRowVerdict,
    plan_dead_row_release,
    release_dead_rows,
)
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
_OWNER = "Owner"
_COLLEAGUE_REPO_PATTERN = r"remote\.git$"


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
        """Reduce the checkout to the one dead shape a single venue can PROVE.

        The admin entry goes, and so does the checkout's own ``.git``: what
        remains claims nothing, so no other execution context can be holding a
        pointer that still resolves. Leaving the gitfile in place would instead
        produce the shape a live checkout created elsewhere presents, which is
        UNKNOWN and releases nothing (#3912).
        """
        admin = self.clone / ".git" / "worktrees" / wt_path.name
        for child in sorted(admin.rglob("*"), reverse=True):
            child.unlink() if child.is_file() else child.rmdir()
        admin.rmdir()
        (wt_path / ".git").unlink()

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

        lines = release_dead_rows(self.workspace, dry_run=False).render()

        assert Worktree.objects.filter(pk=row.pk).exists()
        assert any("KEPT" in line and "unshipped" in line for line in lines), lines

    def test_dry_run_is_the_default_shape_and_removes_nothing(self) -> None:
        row, wt_path = self._dead_row_with_gone_ref()

        lines = release_dead_rows(self.workspace, dry_run=True).render()

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


class AbsentDirectoryTest(_DeadRowCase):
    """A row whose recorded directory is not there is REPORTED, and never released.

    Nothing else owns this shape — the done reaper reads it as an ordinary live row
    — so a pass that dropped it from the plan answered "nothing to do" about the very
    rows an operator reaches for this command to understand. It is also not proof of
    death: a checkout created in another execution context is absent from here in
    exactly the same way, so the row is kept and the reason says why.
    """

    def _row_with_no_directory(self, branch: str = "vanished-dir") -> Worktree:
        return self._register(self.workspace / f"wt-{branch}", branch=branch)

    def test_a_row_whose_directory_is_absent_is_reported(self) -> None:
        row = self._row_with_no_directory()

        verdicts = plan_dead_row_release(self.workspace)

        assert [v.worktree_pk for v in verdicts] == [row.pk]
        assert verdicts[0].disposition is DeadRowDisposition.UNVERIFIABLE

    def test_an_absent_directory_never_authorises_a_release(self) -> None:
        row = self._row_with_no_directory()

        outcome = release_dead_rows(self.workspace, dry_run=False)

        assert Worktree.objects.filter(pk=row.pk).exists()
        assert outcome.released_pks == frozenset()
        assert outcome.render() == [f"KEPT 'vanished-dir' (worktree {row.pk}): {outcome.verdicts[0].reason}"]

    def test_the_reason_names_the_path_and_the_vantage_point(self) -> None:
        self._row_with_no_directory()

        [verdict] = plan_dead_row_release(self.workspace)

        assert str(self.workspace / "wt-vanished-dir") in verdict.reason
        assert "execution context" in verdict.reason


class ProtectionGatesMatchTheSweepTest(_DeadRowCase):
    """The narrow release must protect exactly what ``clean-all`` protects.

    ``reap_done_worktree`` runs ``clean_ignore``, the ownership exclusion and the
    liveness guard BEFORE it classifies the checkout, and none of those signals is
    mooted by the directory dying: ``_db_liveness_reason`` is purely DB-side, and an
    operator's pin or protect-list entry says nothing about the filesystem. A
    release that consulted only the classifier would delete rows the sweep keeps —
    two commands, two standards, the destructive one weaker.

    Each gate is pinned in BOTH directions, so a test cannot pass by the pass
    keeping everything.
    """

    def _release(self, row: Worktree) -> bool:
        release_dead_rows(self.workspace, dry_run=False)
        return not Worktree.objects.filter(pk=row.pk).exists()

    def _verdict(self, branch: str) -> DeadRowVerdict:
        return next(v for v in plan_dead_row_release(self.workspace) if v.branch == branch)

    def test_a_row_whose_ticket_is_busy_is_kept(self) -> None:
        row, _ = self._dead_row_with_gone_ref()
        Session.objects.create(overlay="", ticket=row.ticket)

        assert not self._release(row)

    def test_a_row_whose_session_has_ended_is_still_released(self) -> None:
        row, _ = self._dead_row_with_gone_ref()
        Session.objects.create(overlay="", ticket=row.ticket, ended_at=timezone.now())

        assert self._release(row)

    def test_a_reaper_pinned_row_is_kept(self) -> None:
        row, _ = self._dead_row_with_gone_ref()
        row.extra = {**row.extra, "reaper_pinned": True}
        row.save(update_fields=["extra"])

        assert not self._release(row)

    def test_an_unpinned_row_is_still_released(self) -> None:
        row, _ = self._dead_row_with_gone_ref()
        row.extra = {**row.extra, "reaper_pinned": False}
        row.save(update_fields=["extra"])

        assert self._release(row)

    def test_a_clean_ignore_branch_is_kept(self) -> None:
        row, _ = self._dead_row_with_gone_ref("spike-forever")
        ConfigSetting.objects.set_value("clean_ignore", ["spike-*"])

        assert not self._release(row)

    def test_a_branch_no_clean_ignore_glob_matches_is_still_released(self) -> None:
        row, _ = self._dead_row_with_gone_ref("spike-forever")
        ConfigSetting.objects.set_value("clean_ignore", ["hold/*"])

        assert self._release(row)

    def test_a_colleague_authored_branch_on_a_product_repo_is_kept(self) -> None:
        row = self._pushed_dead_row_authored_by("Colleague")
        ConfigSetting.objects.set_value("colleague_repo_url_pattern", _COLLEAGUE_REPO_PATTERN)

        assert not self._release(row)

    def test_an_owner_authored_branch_on_a_product_repo_is_still_released(self) -> None:
        row = self._pushed_dead_row_authored_by(_OWNER)
        ConfigSetting.objects.set_value("colleague_repo_url_pattern", _COLLEAGUE_REPO_PATTERN)

        assert self._release(row)

    def test_a_protected_row_is_reported_with_the_gate_s_own_reason(self) -> None:
        # The operator has to read WHY it was kept; "holds work" would be a lie here.
        row, _ = self._dead_row_with_gone_ref("pinned")
        row.extra = {**row.extra, "reaper_pinned": True}
        row.save(update_fields=["extra"])

        verdict = self._verdict("pinned")

        assert verdict.disposition is DeadRowDisposition.PROTECTED
        assert "pinned" in verdict.reason

    def test_a_protected_row_is_never_previewed_as_releasable(self) -> None:
        row, _ = self._dead_row_with_gone_ref("busy")
        Session.objects.create(overlay="", ticket=row.ticket)

        lines = release_dead_rows(self.workspace, dry_run=True).render()

        assert not self._verdict("busy").releasable
        assert any("KEPT" in line and "busy" in line for line in lines), lines

    def _pushed_dead_row_authored_by(self, name: str) -> Worktree:
        """A dead-checkout row whose branch survives, so its tip author is resolvable.

        The ownership guard reads the tip author of the branch in the source clone,
        so the gone-ref shape cannot distinguish colleague from owner. The clone's own
        git identity is ``Owner`` and the tip is authored by *name*, which makes the
        split deterministic without depending on whatever identity the host's git
        config carries. Names only: the guard matches either half of the identity, and
        a bare name keeps every address shape out of a public diff.
        """
        branch = f"authored-by-{name.lower()}"
        wt_path = self._worktree_on(branch)
        (wt_path / "work.py").write_text("feat: theirs\n", encoding="utf-8")
        _git(wt_path, "add", "work.py")
        _git(wt_path, "commit", "-q", "--author", f"{name} <>", "-m", "feat: theirs")
        _git(wt_path, "push", "-q", "origin", branch)
        _git(self.clone, "config", "user.name", _OWNER)
        row = self._register(wt_path, branch=branch)
        self._break_checkout(wt_path)
        return row
