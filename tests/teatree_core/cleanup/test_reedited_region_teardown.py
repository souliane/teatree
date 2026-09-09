"""A branch on no remote whose content is NOT on the target is never reclaimed (#4719).

The landed ladder (``branch_redundancy``) answers by patch-id: its synthetic-squash rung
proves the branch's tree-delta ONCE appeared on the target, which a later commit over the
same region does not erase. Two teardown paths accepted that inference alone, so a branch
whose commits are on no remote and whose content will not merge clean onto the target was
reaped: ``cleanup`` at the #706 guard, and the reaper's analysis, which then force-wipes
past every guard.

Real git, because a squash-merge's defining property is that git assigns the content a new
sha and a new patch-id — the exact thing a canned verdict cannot model.
"""

import subprocess
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase

from teatree.core.cleanup.cleanup import CleanupResult, cleanup_worktree
from teatree.core.models import Ticket, Worktree
from teatree.core.worktree.branch_verdict import branch_landed_for_teardown
from teatree.core.worktree.worktree_done import analyze_worktree_changes
from tests.teatree_core.cleanup._shared import _GIT, _clean_env, _run_git, forge_reporting, squash_then_base_evolved

_FEATURE = "feat.txt"


class _ReEditedRegionFixture(TestCase):
    """A bare remote, a main clone, and one linked worktree per test."""

    @pytest.fixture(autouse=True)
    def _workspace(self, tmp_path: Path) -> None:
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir()
        self.bare = tmp_path / "origin.git"
        seed = tmp_path / "seed"
        seed.mkdir()
        self._init(seed)
        self._commit(seed, "base.txt", "base\n", "initial")
        self._git("clone", "--bare", "-q", str(seed), str(self.bare))
        self.repo_main = self.workspace / "myrepo"
        self._git("clone", "-q", str(self.bare), str(self.repo_main))
        self._init_identity(self.repo_main)

    def _git(self, *args: str) -> None:
        subprocess.run([_GIT, *args], check=True, capture_output=True, env=_clean_env())

    def _init(self, path: Path) -> None:
        _run_git("init", "-q", "-b", "main", cwd=path)
        self._init_identity(path)

    def _init_identity(self, path: Path) -> None:
        _run_git("config", "user.email", "t@t", cwd=path)
        _run_git("config", "user.name", "t", cwd=path)

    def _commit(self, cwd: Path, filename: str, content: str, subject: str) -> None:
        (cwd / filename).write_text(content, encoding="utf-8")
        _run_git("add", filename, cwd=cwd)
        _run_git("commit", "-q", "-m", subject, cwd=cwd)

    def _pushed_feature(self, branch: str) -> tuple[Worktree, Path]:
        """A pushed branch carrying TWO commits, so no per-commit patch-id can match the squash."""
        wt_path = self.workspace / branch / "myrepo"
        _run_git("worktree", "add", "-q", "-b", branch, str(wt_path), cwd=self.repo_main)
        self._commit(wt_path, _FEATURE, "v1\n", "feat: add the feature")
        self._commit(wt_path, _FEATURE, "v2\n", "feat: refine the feature")
        _run_git("push", "-q", "origin", f"{branch}:{branch}", cwd=wt_path)
        ticket = Ticket.objects.create(issue_url=f"https://example.com/issues/4719-{branch}")
        worktree = Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="myrepo",
            branch=branch,
            extra={"worktree_path": str(wt_path)},
        )
        return worktree, wt_path

    def _squash_then_drift(self, branch: str, drift: tuple[str, str]) -> None:
        """Squash *branch* onto the remote main, commit *drift*, then forge-delete the source ref."""
        _run_git("merge", "-q", "--squash", branch, cwd=self.repo_main)
        _run_git("commit", "-q", "-m", f"feat: {branch} (#4719)", cwd=self.repo_main)
        self._commit(self.repo_main, drift[0], drift[1], "chore: work after the merge")
        _run_git("push", "-q", "origin", "main", cwd=self.repo_main)
        _run_git("update-ref", "-d", f"refs/heads/{branch}", cwd=self.bare)
        _run_git("fetch", "-q", "--prune", "origin", cwd=self.repo_main)

    def _tip(self, branch: str) -> str:
        out = subprocess.run(
            [_GIT, "-C", str(self.repo_main), "rev-parse", branch],
            check=True,
            capture_output=True,
            text=True,
            env=_clean_env(),
        )
        return out.stdout.strip()

    def _cleanup(self, worktree: Worktree, *, force: bool = False, stub_forge: bool = True) -> CleanupResult:
        overlay = MagicMock()
        overlay.provisioning.cleanup_steps.return_value = []
        with ExitStack() as stack:
            stack.enter_context(patch("teatree.core.cleanup.cleanup.clone_root", return_value=self.workspace))
            stack.enter_context(patch("teatree.core.cleanup.cleanup.get_overlay_for_worktree", return_value=overlay))
            stack.enter_context(patch("teatree.core.cleanup.cleanup.drop_db"))
            stack.enter_context(patch("teatree.core.cleanup.cleanup.remove_postgres_pass_entry"))
            stack.enter_context(patch("teatree.core.cleanup.cleanup.reap_external_resources", return_value=""))
            stack.enter_context(patch("teatree.core.runners.worktree_start.docker_compose_down"))
            if stub_forge:
                stack.enter_context(forge_reporting())
            return cleanup_worktree(worktree, strict_hygiene=False, force=force)


class TestTheTargetReEditedTheSameRegion(_ReEditedRegionFixture):
    """RED before #4719: the landed ladder says redundant, the content is not there."""

    def test_cleanup_refuses_when_merging_back_would_conflict(self) -> None:
        worktree, wt_path = self._pushed_feature("4719-same-region")
        self._squash_then_drift("4719-same-region", drift=(_FEATURE, "rewritten on the target\n"))

        with pytest.raises(RuntimeError, match="NO remote"):
            self._cleanup(worktree)

        assert wt_path.exists(), "a checkout whose content is not on the target must survive"
        assert Worktree.objects.filter(pk=worktree.pk).exists()

    def test_the_reaper_analysis_keeps_it_too(self) -> None:
        """The reaper never reaches the guard above — it force-wipes on this analysis alone."""
        worktree, _ = self._pushed_feature("4719-same-region-reaper")
        self._squash_then_drift("4719-same-region-reaper", drift=(_FEATURE, "rewritten on the target\n"))

        with forge_reporting():
            analysis = analyze_worktree_changes(worktree, workspace=self.workspace)

        assert analysis.proven_redundant is False
        assert any("not provably on" in reason for reason in analysis.kept_reasons)


class TestLandedWorkIsStillReclaimed(_ReEditedRegionFixture):
    """Controls: these fail if the fix simply disables the reclaim."""

    def test_cleanup_reclaims_when_the_drift_is_on_another_file(self) -> None:
        worktree, wt_path = self._pushed_feature("4719-other-file")
        self._squash_then_drift("4719-other-file", drift=("base.txt", "base\nlater work\n"))

        result = self._cleanup(worktree)

        assert result.errors == [], f"squash-landed worktree reported errors: {result.errors}"
        assert not wt_path.exists()
        assert not Worktree.objects.filter(pk=worktree.pk).exists()

    def test_the_reaper_analysis_still_proves_it_redundant(self) -> None:
        worktree, _ = self._pushed_feature("4719-other-file-reaper")
        self._squash_then_drift("4719-other-file-reaper", drift=("base.txt", "base\nlater work\n"))

        with forge_reporting():
            analysis = analyze_worktree_changes(worktree, workspace=self.workspace)

        assert analysis.proven_redundant is True, f"kept for: {analysis.kept_reasons}"

    def test_force_remains_the_escape_for_the_refused_case(self) -> None:
        worktree, wt_path = self._pushed_feature("4719-forced")
        self._squash_then_drift("4719-forced", drift=(_FEATURE, "rewritten on the target\n"))

        result = self._cleanup(worktree, force=True)

        assert result.errors == [], f"force teardown reported errors: {result.errors}"
        assert not wt_path.exists()


class TestTheForgeRecordAtTheExactTipStillStandsAlone:
    """#4423's rung is preserved: it is a forge RECORD of these exact bytes, not an inference.

    ``squash_then_base_evolved`` is the shape that isolates it — the squash was resolved at
    merge and the base then rewrote the file, so every git-local rung reads NOT landed and
    merging back conflicts. Applying the presence conjunct to this rung too would make the
    branch permanently unreclaimable, which is the pain #4423 fixed.
    """

    def test_a_merged_pr_at_the_exact_tip_authorises_the_teardown(self, tmp_path: Path) -> None:
        work, tip = squash_then_base_evolved(tmp_path)

        with forge_reporting(merged_head_sha=tip):
            assert branch_landed_for_teardown(str(work), "feature", "origin/main") is True

    def test_without_that_record_the_same_branch_is_refused(self, tmp_path: Path) -> None:
        work, _tip = squash_then_base_evolved(tmp_path)

        with forge_reporting():
            assert branch_landed_for_teardown(str(work), "feature", "origin/main") is False
