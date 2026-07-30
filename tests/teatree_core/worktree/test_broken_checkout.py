"""One owner for a registered worktree whose checkout is dead (souliane/teatree#3583 follow-up).

The deadlock this pins: the doctor FAILed on a registered ``Worktree`` row whose
dir exists but fails ``git rev-parse``, and prescribed ``workspace clean-all`` —
whose broken-DIR pass skipped that very dir because a row still tracked it, while
the ROW pass could never clear it (its dirt probe runs git INSIDE the dead dir).
Every item the doctor could flag was by construction tracked, so the prescribed
remedy was a no-op for 100% of them.

Functional throughout: real clones, real ``git worktree add``, real rows, and the
real ``run_clean_all`` body the doctor's remedy runs.
"""

import os
import tempfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from django.test import TestCase

import teatree.core.management.commands._workspace.clean_all as ws_clean_all_mod
from teatree.cli.doctor.checks_worktree_health import _check_registered_worktrees_are_checkouts
from teatree.core.models import Ticket, Worktree
from teatree.core.worktree.broken_checkout import BrokenCheckout, classify_broken_checkout
from teatree.core.worktree.worktree_done import reap_done_worktree
from teatree.utils import git
from teatree.utils.run import CommandFailedError, run_checked

# Backdated so the liveness guard's recent-commit signal never fires and masks
# the disposition under test.
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
    # GIT_* is stripped: under the inline pre-commit pytest hook the outer commit
    # exports GIT_DIR/GIT_WORK_TREE, which would hijack these onto the real repo.
    hermetic = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    run_checked(["git", "-C", str(repo), *args], env={**hermetic, **_GIT_ENV})


def _clone_with_remote(root: Path) -> Path:
    """A work clone whose ``origin`` is a real bare repo, with ``main`` pushed."""
    remote = root / "remote.git"
    _git(root, "init", "--quiet", "--bare", "--initial-branch=main", str(remote))
    work = root / "backend"
    _git(root, "clone", "--quiet", str(remote), str(work))
    (work / "README.md").write_text("base\n", encoding="utf-8")
    _git(work, "add", "README.md")
    _git(work, "commit", "-q", "-m", "chore: base")
    _git(work, "push", "-q", "origin", "main")
    return work


def _prune_admin_entry(clone: Path, wt_path: Path) -> None:
    """Prune the worktree's git admin entry, leaving the checkout's gitfile pointing at nothing."""
    admin = clone / ".git" / "worktrees" / wt_path.name
    for child in sorted(admin.rglob("*"), reverse=True):
        child.unlink() if child.is_file() else child.rmdir()
    admin.rmdir()


def _break_checkout(clone: Path, wt_path: Path) -> None:
    """Reduce the checkout to the one dead shape a single venue can PROVE.

    The admin entry goes, and so does the checkout's own ``.git``. What is left
    claims nothing: git reports no repository, and there is no pointer that some
    other execution context might still resolve. That is the whole standard for
    ``NOT_A_CHECKOUT``, and the only state a release may rest on.
    """
    _prune_admin_entry(clone, wt_path)
    (wt_path / ".git").unlink()


class _BrokenWorktreeCase(TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.workspace = self.tmp / "workspace"
        self.workspace.mkdir()
        self.clone = _clone_with_remote(self.workspace)

    def _register(self, wt_path: Path, *, branch: str, clone: Path | None = None) -> Worktree:
        ticket = Ticket.objects.create(overlay="", issue_url=f"https://example.invalid/org/repo/issues/{branch}")
        return Worktree.objects.create(
            overlay="",
            ticket=ticket,
            repo_path="backend",
            branch=branch,
            extra={"clone_path": str(clone or self.clone), "worktree_path": str(wt_path)},
        )

    def _worktree_on(self, branch: str, *, commit: str | None = None) -> Path:
        """Add a real worktree on a new *branch*, optionally carrying one commit."""
        wt_path = self.workspace / f"wt-{branch}"
        _git(self.clone, "worktree", "add", "-q", "-b", branch, str(wt_path))
        if commit is not None:
            (wt_path / "work.py").write_text(f"{commit}\n", encoding="utf-8")
            _git(wt_path, "add", "work.py")
            _git(wt_path, "commit", "-q", "-m", commit)
        return wt_path

    def _merged_broken_worktree(self, branch: str = "shipped") -> tuple[Worktree, Path]:
        """A registered row whose branch shipped and whose checkout is now dead."""
        wt_path = self._worktree_on(branch, commit="feat: shipped work")
        _git(wt_path, "push", "-q", "origin", branch)
        _git(self.clone, "checkout", "-q", "main")
        _git(self.clone, "merge", "-q", "--squash", branch)
        _git(self.clone, "commit", "-q", "-m", "feat: shipped work via squash")
        _git(self.clone, "push", "-q", "origin", "main")
        _git(self.clone, "fetch", "-q", "origin")
        row = self._register(wt_path, branch=branch)
        _break_checkout(self.clone, wt_path)
        return row, wt_path


def _doctor_checkout_verdict() -> tuple[bool, str]:
    buf = StringIO()
    with redirect_stdout(buf):
        ok = _check_registered_worktrees_are_checkouts()
    return ok, buf.getvalue()


def _nothing(*_args: object, **_kwargs: object) -> list[str]:
    return []


_CLEAN_ALL = "teatree.core.management.commands._workspace.clean_all"
_UNRELATED_PASSES = (
    f"{_CLEAN_ALL}.prune_branches",
    f"{_CLEAN_ALL}.drop_orphaned_stashes",
    f"{_CLEAN_ALL}.drop_orphan_databases",
    f"{_CLEAN_ALL}.reap_orphan_worktree_docker",
    f"{_CLEAN_ALL}.reap_orphan_isolated_worktree_roots",
    f"{_CLEAN_ALL}.reap_orphan_raw_worktrees",
    "teatree.utils.django_db.prune_dslr_snapshots",
    "teatree.core.runners.worktree_start.docker_compose_down",
)


class TheDoctorRemedyClosesTheLoopTest(_BrokenWorktreeCase):
    """The cross-seam contract: the command the doctor NAMES resolves what it flagged."""

    def _run_remedy(self) -> list[str]:
        """``workspace clean-all``'s body, with only the two passes under test live."""
        for target in _UNRELATED_PASSES:
            patcher = mock.patch(target, new=_nothing)
            patcher.start()
            self.addCleanup(patcher.stop)
        io = ws_clean_all_mod.CleanAllIO(write_out=lambda _line: None, write_err=lambda _line: None)
        return ws_clean_all_mod.run_clean_all(self.workspace, io, keep_dslr=3, dry_run=False)

    def test_a_broken_registered_checkout_is_released_by_the_prescribed_remedy(self) -> None:
        row, wt_path = self._merged_broken_worktree()

        red, message = _doctor_checkout_verdict()
        assert red is False, "the doctor must FAIL on a registered row whose dir never was a checkout"
        assert "workspace clean-all" in message, "the remedy the doctor prints is the one under test"

        lines = self._run_remedy()

        assert not Worktree.objects.filter(pk=row.pk).exists(), f"the row was never released: {lines}"
        # The DIRECTORY is not the remedy's to remove. Its files were never proven
        # redundant by anything the sweep read, so disposing of them stays an
        # explicit operator decision (#3912).
        assert wt_path.is_dir(), f"the remedy removed a directory it had no proof about: {lines}"
        green, after = _doctor_checkout_verdict()
        assert green is True, f"the doctor is still RED after its own remedy: {after}"


class FailClosedControlsTest(_BrokenWorktreeCase):
    """A release needs POSITIVE proof; every uncertainty keeps the row."""

    def test_a_checkout_whose_pointer_only_its_creator_can_resolve_is_never_released(self) -> None:
        # #3912/#3853: the admin entry is gone from THIS clone, and the checkout
        # still names one. That is exactly what a healthy checkout created in
        # another execution context looks like from here, so it is UNKNOWN — the
        # branch is never even consulted, and no release is authorised.
        wt_path = self._worktree_on("elsewhere", commit="feat: work")
        row = self._register(wt_path, branch="elsewhere")
        _prune_admin_entry(self.clone, wt_path)

        verdict = classify_broken_checkout(row, workspace=self.workspace)

        assert verdict.state is BrokenCheckout.UNVERIFIABLE
        assert "does not exist in this execution context" in verdict.reason

    def test_a_checkout_the_visible_clone_still_vouches_for_is_live(self) -> None:
        # The false-dead report itself: the checkout's recorded admin dir is
        # unreachable, but the clone this venue CAN see holds its entry.
        wt_path = self._worktree_on("relocated", commit="feat: work")
        row = self._register(wt_path, branch="relocated")
        (wt_path / ".git").write_text(
            f"gitdir: /nonexistent/other-context/.git/worktrees/{wt_path.name}\n", encoding="utf-8"
        )

        verdict = classify_broken_checkout(row, workspace=self.workspace)

        assert verdict.state is BrokenCheckout.LIVE_CHECKOUT

    def test_a_healthy_checkout_is_never_a_release_candidate(self) -> None:
        wt_path = self._worktree_on("live", commit="feat: in progress")
        row = self._register(wt_path, branch="live")

        verdict = classify_broken_checkout(row, workspace=self.workspace)

        assert verdict.state is BrokenCheckout.LIVE_CHECKOUT

    def test_an_inconclusive_probe_is_not_proof_and_keeps_the_row(self) -> None:
        row, _wt_path = self._merged_broken_worktree()
        # git's "dubious ownership" refusal: the checkout may be perfectly fine,
        # git just declines to speak — never a licence to release.
        refusal = mock.Mock(returncode=128, stdout="", stderr="fatal: detected dubious ownership in repository")

        with mock.patch("teatree.core.worktree.worktree_roots.run_allowed_to_fail", return_value=refusal):
            verdict = classify_broken_checkout(row, workspace=self.workspace)

        assert verdict.state is BrokenCheckout.UNVERIFIABLE
        assert "could not" in verdict.reason

    def test_a_branch_holding_unpushed_commits_is_kept_for_salvage(self) -> None:
        wt_path = self._worktree_on("unshipped", commit="feat: never pushed")
        row = self._register(wt_path, branch="unshipped")
        _break_checkout(self.clone, wt_path)

        verdict = classify_broken_checkout(row, workspace=self.workspace)

        assert verdict.state is BrokenCheckout.HOLDS_WORK
        assert "NO remote" in verdict.reason

    def test_a_push_state_git_could_not_read_keeps_the_row(self) -> None:
        wt_path = self._worktree_on("unreadable", commit="feat: work")
        row = self._register(wt_path, branch="unreadable")
        _break_checkout(self.clone, wt_path)
        boom = CommandFailedError(["git", "log"], 128, "", "fatal: bad object")

        with mock.patch.object(git, "commits_absent_from_all_remotes", side_effect=boom):
            verdict = classify_broken_checkout(row, workspace=self.workspace)

        assert verdict.state is BrokenCheckout.HOLDS_WORK
        assert "could not read" in verdict.reason

    def test_an_unresolvable_clone_leaves_the_branch_unverifiable(self) -> None:
        wt_path = self._worktree_on("orphan", commit="feat: work")
        row = self._register(wt_path, branch="orphan", clone=self.tmp / "clone-is-gone")
        row.repo_path = "no-such-repo"  # nothing for the fallback scan to find either
        row.save(update_fields=["repo_path"])
        _break_checkout(self.clone, wt_path)

        verdict = classify_broken_checkout(row, workspace=self.workspace)

        assert verdict.state is BrokenCheckout.UNVERIFIABLE
        assert "clone" in verdict.reason

    def test_a_stale_stored_clone_path_no_longer_blinds_the_branch_probe(self) -> None:
        wt_path = self._worktree_on("unshipped", commit="feat: never pushed")
        row = self._register(wt_path, branch="unshipped", clone=self.tmp / "clone-moved-away")
        _break_checkout(self.clone, wt_path)

        verdict = classify_broken_checkout(row, workspace=self.workspace)

        assert verdict.state is BrokenCheckout.HOLDS_WORK, "the real clone is discoverable, so the branch is readable"
        assert "NO remote" in verdict.reason

    def test_a_row_with_no_dir_on_disk_is_not_this_pass_business(self) -> None:
        row = self._register(self.workspace / "never-existed", branch="ghost")

        verdict = classify_broken_checkout(row, workspace=self.workspace)

        assert verdict.state is BrokenCheckout.LIVE_CHECKOUT


class RowReaperDispositionTest(_BrokenWorktreeCase):
    """How the row reaper reports the dead-checkout branch it now owns."""

    def test_a_dry_run_previews_the_release_and_removes_nothing(self) -> None:
        row, wt_path = self._merged_broken_worktree()

        outcome = reap_done_worktree(row, workspace=self.workspace, dry_run=True)

        assert outcome.action == "would-wipe"
        assert "WOULD RELEASE" in outcome.label
        assert Worktree.objects.filter(pk=row.pk).exists()
        assert wt_path.is_dir()

    def test_a_dead_checkout_holding_work_is_kept_and_points_at_salvage(self) -> None:
        wt_path = self._worktree_on("keepme", commit="feat: never pushed")
        row = self._register(wt_path, branch="keepme")
        _break_checkout(self.clone, wt_path)

        outcome = reap_done_worktree(row, workspace=self.workspace, dry_run=False)

        assert outcome.action == "kept"
        assert "workspace salvage" in outcome.label
        assert outcome.emit is not None
        assert Worktree.objects.filter(pk=row.pk).exists()


class ReleasableWithoutARemoteCopyTest(_BrokenWorktreeCase):
    """Positive proof also comes from CONTENT, not only from a remote-reachable SHA."""

    def test_a_never_pushed_branch_whose_content_squash_landed_is_releasable(self) -> None:
        # Its commits are on NO remote by SHA (the squash rewrote them), so only the
        # patch-id content gate can authorise the release.
        wt_path = self._worktree_on("squashed", commit="feat: content that landed")
        _git(self.clone, "checkout", "-q", "main")
        _git(self.clone, "merge", "-q", "--squash", "squashed")
        _git(self.clone, "commit", "-q", "-m", "feat: content that landed via squash")
        _git(self.clone, "push", "-q", "origin", "main")
        _git(self.clone, "fetch", "-q", "origin")
        row = self._register(wt_path, branch="squashed")
        _break_checkout(self.clone, wt_path)

        verdict = classify_broken_checkout(row, workspace=self.workspace)

        assert verdict.state is BrokenCheckout.RELEASABLE

    def test_a_row_whose_branch_ref_is_gone_is_releasable(self) -> None:
        wt_path = self._worktree_on("deleted-ref", commit="feat: work")
        row = self._register(wt_path, branch="deleted-ref")
        _break_checkout(self.clone, wt_path)
        _git(self.clone, "branch", "-D", "deleted-ref")

        verdict = classify_broken_checkout(row, workspace=self.workspace)

        assert verdict.state is BrokenCheckout.RELEASABLE
        assert "branch ref is gone" in verdict.reason


class StaleTrackingRefsCannotAuthoriseAReleaseTest(_BrokenWorktreeCase):
    """The freshness precondition on the #706 guard, on the row-reaper path.

    ``commits_absent_from_all_remotes`` is a purely local graph query over
    ``refs/remotes/*``. A tracking ref goes STALE the moment its branch is deleted
    upstream by anything other than this clone — the ordinary forge
    auto-delete-on-merge — and against a stale ref the guard answers "pushed" for
    commits that exist on NO remote. That is the misread that authorises reaping the
    last copy of unmerged work. The branch-prune and raw-orphan passes already
    refresh first; this pins the same precondition on the dead-checkout row path,
    which is the one the doctor's finding routes through.
    """

    def _pushed_then_deleted_upstream(self, branch: str = "vanished") -> tuple[Worktree, Path]:
        """A branch pushed, then deleted on the remote WITHOUT pruning locally.

        The delete is applied to the BARE REMOTE directly, never via this clone's own
        ``push --delete`` — that would prune the tracking ref as a side effect and
        destroy the very staleness under test. Deleting server-side is also what
        really happens: the forge's auto-delete-on-merge, or a sibling clone. Leaves
        ``refs/remotes/origin/<branch>`` present-but-stale, so an unrefreshed probe
        reads the commit as safely on a remote when it is on none.
        """
        wt_path = self._worktree_on(branch, commit="feat: the only copy")
        _git(wt_path, "push", "-q", "origin", branch)
        _git(self.workspace / "remote.git", "update-ref", "-d", f"refs/heads/{branch}")
        assert git.run(repo=str(self.clone), args=["rev-parse", "--verify", "--quiet", f"origin/{branch}"]), (
            "the test premise is a STALE tracking ref — it must still be present locally"
        )
        row = self._register(wt_path, branch=branch)
        _break_checkout(self.clone, wt_path)
        return row, wt_path

    def test_control_the_unrefreshed_probe_really_does_misread_the_stale_ref(self) -> None:
        # The control that makes the next test falsifiable: without a refresh the
        # #706 primitive reports NOTHING absent from remotes for a commit on no remote.
        self._pushed_then_deleted_upstream()

        assert git.commits_absent_from_all_remotes(str(self.clone), "vanished") == []

    def test_a_stale_tracking_ref_does_not_authorise_a_release(self) -> None:
        row, _wt_path = self._pushed_then_deleted_upstream()

        verdict = classify_broken_checkout(row, workspace=self.workspace)

        assert verdict.state is BrokenCheckout.HOLDS_WORK, (
            "a commit whose only remote copy was deleted upstream must never read as pushed"
        )

    def test_a_failed_refresh_keeps_the_row_rather_than_judging_on_stale_refs(self) -> None:
        row, _wt_path = self._merged_broken_worktree()

        with mock.patch.object(git, "fetch_all_prune", return_value=False):
            verdict = classify_broken_checkout(row, workspace=self.workspace)

        assert verdict.state is BrokenCheckout.UNVERIFIABLE
        assert "refresh" in verdict.reason

    def test_a_gone_branch_ref_needs_no_network_and_is_still_releasable_offline(self) -> None:
        # No ref means no commits to lose, so the freshness precondition has nothing
        # to protect — an offline host must still be able to clear these rows.
        wt_path = self._worktree_on("deleted-ref", commit="feat: work")
        row = self._register(wt_path, branch="deleted-ref")
        _break_checkout(self.clone, wt_path)
        _git(self.clone, "branch", "-D", "deleted-ref")

        with mock.patch.object(git, "fetch_all_prune", return_value=False):
            verdict = classify_broken_checkout(row, workspace=self.workspace)

        assert verdict.state is BrokenCheckout.RELEASABLE
