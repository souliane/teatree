"""Teardown must reclaim a worktree whose branch content landed via a SQUASH merge (#4423).

Both data-loss guards judged "is this landed?" with blind primitives: ``_raise_if_unpushed``
through exact tree equality with ``origin/<default>``, ``_raise_if_genuinely_ahead`` through
per-commit patch-ids. A squash rewrites the branch's commits into one new sha, so patch-ids stop
matching, and any later commit on the target breaks tree equality — measured on this deployment
as a sweep that refused EVERY candidate and reclaimed nothing.

The falsifiable criteria are the two reclaim tests: they fail against the unmodified guards and
can only pass once the layered verdict is consulted. The refusal tests beside them are guards,
not proofs — they pass before the change too, and exist to fail if the fix widens the accept-set
past landed work. The #2205 case is the sharp one: that branch's tree MATCHES the target by
coincidence, so a presence-only fix passes every reclaim test here and still destroys the only
copy of the work.
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.cleanup.cleanup import CleanupResult, cleanup_worktree
from teatree.core.models import Ticket, Worktree
from tests.teatree_core.cleanup._shared import _GIT, _clean_env, _run_git

_FEATURE = "feat.txt"


def _clone(src: Path, dest: Path, *bare: str) -> None:
    subprocess.run([_GIT, "clone", *bare, "-q", str(src), str(dest)], check=True, capture_output=True, env=_clean_env())


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _run_git("init", "-q", "-b", "main", cwd=path)
    _run_git("config", "user.email", "t@t", cwd=path)
    _run_git("config", "user.name", "t", cwd=path)


class _SquashLandedFixture(TestCase):
    """A real bare-remote topology plus one linked worktree per test.

    Real git, not a mock: a squash-merge's defining property is that git assigns the content a
    new sha and a new patch-id, which is precisely what a canned classification cannot model.
    """

    @pytest.fixture(autouse=True)
    def _workspace(self, tmp_path: Path) -> None:
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir()
        self.bare = tmp_path / "origin.git"
        seed = tmp_path / "seed"
        _init_repo(seed)
        (seed / "base.txt").write_text("base\n")
        _run_git("add", "base.txt", cwd=seed)
        _run_git("commit", "-q", "-m", "initial", cwd=seed)
        _clone(seed, self.bare, "--bare")
        self.repo_main = self.workspace / "myrepo"
        _clone(self.bare, self.repo_main)
        _run_git("config", "user.email", "t@t", cwd=self.repo_main)
        _run_git("config", "user.name", "t", cwd=self.repo_main)

    def _worktree_on(self, branch: str) -> tuple[Worktree, Path]:
        wt_path = self.workspace / branch / "myrepo"
        _run_git("worktree", "add", "-q", "-b", branch, str(wt_path), cwd=self.repo_main)
        ticket = Ticket.objects.create(
            issue_url=f"https://example.com/issues/4423-{branch}",
            state=Ticket.State.IN_REVIEW,
        )
        worktree = Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="myrepo",
            branch=branch,
            extra={"worktree_path": str(wt_path)},
        )
        return worktree, wt_path

    def _commit(self, cwd: Path, filename: str, content: str, subject: str) -> None:
        (cwd / filename).write_text(content)
        _run_git("add", filename, cwd=cwd)
        _run_git("commit", "-q", "-m", subject, cwd=cwd)

    def _two_commit_feature(self, branch: str) -> tuple[Worktree, Path]:
        """A pushed branch carrying TWO commits, which is what makes both guards fire.

        A squash folds them into one commit whose patch-id matches neither, so ``git cherry``
        reports both as blockers — the strict guard's authoritative gate — while the single-commit
        shape would slip through it and leave that guard untested.
        """
        worktree, wt_path = self._worktree_on(branch)
        self._commit(wt_path, _FEATURE, "v1\n", "feat: add the feature")
        self._commit(wt_path, _FEATURE, "v2\n", "feat: refine the feature")
        _run_git("push", "-q", "origin", f"{branch}:{branch}", cwd=wt_path)
        return worktree, wt_path

    def _squash_onto_main(self, branch: str, *, drift: tuple[str, str] | None) -> None:
        """Squash *branch* onto the remote's main, optionally commit *drift*, then forge-delete the ref.

        Deleting the source ref on the bare remote and pruning models what a forge does on
        merge: the branch's commits then exist on no remote ref at all, which is the state the
        #706 guard reads as unrecoverable work.
        """
        _run_git("merge", "-q", "--squash", branch, cwd=self.repo_main)
        _run_git("commit", "-q", "-m", f"feat: {branch} (#4423)", cwd=self.repo_main)
        if drift is not None:
            self._commit(self.repo_main, drift[0], drift[1], "chore: work after the merge")
        _run_git("push", "-q", "origin", "main", cwd=self.repo_main)
        _run_git("update-ref", "-d", f"refs/heads/{branch}", cwd=self.bare)
        _run_git("fetch", "-q", "--prune", "origin", cwd=self.repo_main)

    def _cleanup(self, worktree: Worktree, *, strict_hygiene: bool, force: bool = False) -> CleanupResult:
        with (
            patch("teatree.core.cleanup.cleanup.clone_root", return_value=self.workspace),
            patch("teatree.core.cleanup.cleanup.get_overlay_for_worktree") as mock_overlay,
        ):
            mock_overlay.return_value.provisioning.cleanup_steps.return_value = []
            return cleanup_worktree(worktree, strict_hygiene=strict_hygiene, force=force)


class TestSquashMergedWorktreeIsReclaimed(_SquashLandedFixture):
    """The defect: a squash-merged branch is unreclaimable once the target moves on."""

    def test_reclaims_after_unrelated_post_merge_drift(self) -> None:
        """The FSM teardown path (``strict_hygiene=False``) — RED before #4423."""
        worktree, wt_path = self._two_commit_feature("4423-reclaim")
        self._squash_onto_main("4423-reclaim", drift=("base.txt", "base\nlater work\n"))

        result = self._cleanup(worktree, strict_hygiene=False)

        assert result.clean is True, f"squash-merged worktree reported errors: {result.errors}"
        assert not wt_path.exists(), "a worktree whose content is on the target must be reclaimed"
        assert not Worktree.objects.filter(pk=worktree.pk).exists()

    def test_strict_hygiene_reclaims_after_unrelated_post_merge_drift(self) -> None:
        """The ``clean-all`` / sync-backend path (``strict_hygiene=True``) — RED before #4423.

        Distinct from its sibling: this one is refused by ``_raise_if_genuinely_ahead``'s
        patch-id gate rather than by the tree-equality override, so a fix applied to only one
        guard leaves this red.
        """
        worktree, wt_path = self._two_commit_feature("4423-reclaim-strict")
        self._squash_onto_main("4423-reclaim-strict", drift=("base.txt", "base\nlater work\n"))

        result = self._cleanup(worktree, strict_hygiene=True)

        assert result.clean is True, f"squash-merged worktree reported errors: {result.errors}"
        assert not wt_path.exists(), "the strict hygiene gate must also reclaim landed content"
        assert not Worktree.objects.filter(pk=worktree.pk).exists()


class TestUnlandedWorkIsStillRefused(_SquashLandedFixture):
    """Guards, not proofs: these pass before the change and fail if it over-widens."""

    def test_refuses_a_genuinely_unpushed_branch_and_force_remains_the_escape(self) -> None:
        worktree, wt_path = self._worktree_on("4423-unpushed")
        self._commit(wt_path, _FEATURE, "never pushed\n", "feat: genuinely unpushed")

        with pytest.raises(RuntimeError, match="NO remote"):
            self._cleanup(worktree, strict_hygiene=False)
        assert wt_path.exists(), "unpushed work must survive a non-forced teardown"

        result = self._cleanup(worktree, strict_hygiene=False, force=True)

        assert result.clean is True, f"force teardown reported errors: {result.errors}"
        assert not wt_path.exists(), "force must remain the escape for the ambiguous case"

    def test_refuses_a_local_only_branch_whose_work_was_reverted(self) -> None:
        """#2205 — the tree matches the target by coincidence, yet no copy exists anywhere else.

        This is the case tree equality alone gets wrong, and the reason the fix cannot be a
        present-tense check on its own: merging this branch into the target changes nothing, so
        presence says landed while the work exists on no remote at all.
        """
        worktree, wt_path = self._worktree_on("4423-reverted")
        self._commit(wt_path, _FEATURE, "work\n", "feat: local work")
        (wt_path / _FEATURE).unlink()
        _run_git("add", "-A", cwd=wt_path)
        _run_git("commit", "-q", "-m", "revert: back it out", cwd=wt_path)

        with pytest.raises(RuntimeError, match="NO remote"):
            self._cleanup(worktree, strict_hygiene=False)

        assert wt_path.exists(), "a never-pushed branch must be kept however its tree compares"
        assert Worktree.objects.filter(pk=worktree.pk).exists()

    def test_keeps_the_worktree_when_the_target_re_edited_the_same_region(self) -> None:
        """The known trade, pinned rather than discovered.

        Merging back would revert the target's own follow-up edit, so the merge conflicts and
        presence reads false. The worktree is KEPT — the safe direction, but it means the reclaim
        above holds for drift on OTHER files, not for drift over the branch's own lines.
        """
        worktree, wt_path = self._two_commit_feature("4423-same-region")
        self._squash_onto_main("4423-same-region", drift=(_FEATURE, "rewritten on the target\n"))

        with pytest.raises(RuntimeError, match="NO remote"):
            self._cleanup(worktree, strict_hygiene=False)

        assert wt_path.exists(), "an unmergeable region is not proof the content is present"
