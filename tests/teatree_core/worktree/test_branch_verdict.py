"""``branch_verdict`` — the read-only front door for "is this branch's work on main?" (#4070).

Real git under ``tmp_path``: the whole point is that a squash-merge rewrites shas, so a
mocked git call cannot reproduce the condition the layered classifier exists to see.

The report must serialize ``forge_merged``, ``merged_with_post_merge_work`` and
``unique_shas`` TOGETHER. A caller told only "merged" would read it as "safe to delete",
and the delta is exactly the post-merge work the salvage path routes to a fresh PR.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.worktree import branch_classification
from teatree.core.worktree.branch_verdict import branch_is_landed, branch_verdict_report, render_verdict
from tests._git_repo import make_git_repo, run_git


def _commit(repo: Path, name: str, body: str) -> None:
    (repo / name).write_text(body)
    run_git(repo, "add", name)
    run_git(repo, "commit", "-q", "-m", f"add {name}")


def _clone_with_origin(root: Path, *, default_branch: str = "main") -> Path:
    """A working clone whose ``origin/<default>`` is a real remote-tracking ref.

    The identity is pinned in the repo, not in the fixture builder's environment: the
    synthetic-squash layer builds a probe commit with ``git commit-tree``, which the
    production code runs under the ambient environment.
    """
    origin = make_git_repo(root / "origin", default_branch=default_branch, bare=True)
    work = make_git_repo(root / "work", default_branch=default_branch)
    run_git(work, "config", "user.name", "Test")
    run_git(work, "config", "user.email", "test@example.com")
    _commit(work, "README.md", "base\n")
    run_git(work, "remote", "add", "origin", str(origin))
    run_git(work, "push", "-q", "origin", default_branch)
    run_git(work, "fetch", "-q", "origin")
    return work


def _squash_merge(repo: Path, branch: str, message: str) -> None:
    """Land *branch*'s whole tree-delta on ``main`` as ONE new commit, then publish it."""
    run_git(repo, "checkout", "-q", "main")
    run_git(repo, "merge", "-q", "--squash", branch)
    run_git(repo, "commit", "-q", "-m", message)
    run_git(repo, "push", "-q", "origin", "main")
    run_git(repo, "fetch", "-q", "origin")


def _publish_main(repo: Path) -> None:
    run_git(repo, "push", "-q", "origin", "main")
    run_git(repo, "fetch", "-q", "origin")


class BranchVerdictCase(TestCase):
    @pytest.fixture(autouse=True)
    def _tmp_root(self, tmp_path: Path) -> None:
        self.root = tmp_path

    def _repo_with_branch(self, *commits: str) -> Path:
        repo = _clone_with_origin(self.root)
        run_git(repo, "checkout", "-q", "-b", "feature")
        for name in commits:
            _commit(repo, name, f"{name}\n")
        return repo


class TestASquashMergedBranchReadsAsLanded(BranchVerdictCase):
    def test_redundant_with_the_deciding_layer_named(self) -> None:
        repo = self._repo_with_branch("one.py", "two.py")
        _squash_merge(repo, "feature", "feat: one and two (#1)")

        report = branch_verdict_report(str(repo), "feature")

        assert report.redundant is True
        assert report.source in {"cherry-zero-unique", "synthetic-squash", "branch-merged"}
        assert report.target == "origin/main"
        assert report.branch == "feature"

    def test_branch_is_landed_is_the_boolean_view(self) -> None:
        repo = self._repo_with_branch("one.py")
        _squash_merge(repo, "feature", "feat: one (#1)")

        assert branch_is_landed(str(repo), "feature") is True


class TestAnUnmergedBranchReadsAsNotLanded(BranchVerdictCase):
    def test_not_redundant_and_its_unique_shas_are_reported(self) -> None:
        repo = self._repo_with_branch("one.py")

        report = branch_verdict_report(str(repo), "feature")

        assert report.redundant is False
        assert report.merged_with_post_merge_work is False
        assert len(report.unique_shas) == 1
        assert branch_is_landed(str(repo), "feature") is False


class TestMergedIsNeverReadableAsSafeToDelete(BranchVerdictCase):
    def test_post_merge_work_is_surfaced_beside_the_forge_merged_signal(self) -> None:
        repo = self._repo_with_branch("one.py")
        _squash_merge(repo, "feature", "feat: one (#1)")
        run_git(repo, "checkout", "-q", "feature")
        _commit(repo, "later.py", "AFTER THE MERGE\n")

        with patch.object(branch_classification, "_branch_pr_is_merged", return_value=True):
            report = branch_verdict_report(str(repo), "feature")

        assert report.forge_merged is True
        assert report.redundant is False
        assert report.merged_with_post_merge_work is True
        assert report.unique_shas, "the post-merge delta must be named, not silently dropped"


class TestAPatchRevertedOnTheTargetIsNotLanded(BranchVerdictCase):
    """The patch-id layers read a patch's PRIOR appearance, which a revert does not erase.

    Left uncorrected, a squash-merged-then-reverted branch reads as LANDED, so ``ship``
    refuses the fresh PR that would re-land it and the re-push is stranded — the exact
    direction the #776 refusal calls unacceptable.
    """

    def test_a_squash_merge_then_revert_reads_as_not_landed(self) -> None:
        repo = self._repo_with_branch("one.py")
        _squash_merge(repo, "feature", "feat: one (#1)")
        run_git(repo, "checkout", "-q", "main")
        run_git(repo, "revert", "--no-edit", "HEAD")
        _publish_main(repo)

        assert branch_is_landed(str(repo), "feature") is False

    def test_the_front_door_reports_the_absent_content_beside_the_stale_layers(self) -> None:
        repo = self._repo_with_branch("one.py")
        _squash_merge(repo, "feature", "feat: one (#1)")
        run_git(repo, "checkout", "-q", "main")
        run_git(repo, "revert", "--no-edit", "HEAD")
        _publish_main(repo)

        report = branch_verdict_report(str(repo), "feature")

        assert report.content_present_on_target is False
        assert report.redundant is True, "the layered verdict is unchanged — presence is the new, separate layer"
        assert "NOT on origin/main now" in render_verdict(report)

    def test_post_merge_drift_on_unrelated_files_stays_landed(self) -> None:
        repo = self._repo_with_branch("one.py")
        _squash_merge(repo, "feature", "feat: one (#1)")
        run_git(repo, "checkout", "-q", "main")
        _commit(repo, "unrelated.py", "moved on since\n")
        _publish_main(repo)

        assert branch_is_landed(str(repo), "feature") is True

    def test_post_merge_drift_on_the_same_region_reads_as_not_landed(self) -> None:
        """The accepted trade: an unmergeable region is not provable presence, so ship opens a PR."""
        repo = self._repo_with_branch("one.py")
        _squash_merge(repo, "feature", "feat: one (#1)")
        run_git(repo, "checkout", "-q", "main")
        (repo / "one.py").write_text("rewritten on main\n")
        run_git(repo, "commit", "-q", "-am", "refactor: rewrite one.py")
        _publish_main(repo)

        assert branch_is_landed(str(repo), "feature") is False


class TestTheBooleanPathPaysNoForgeProbe(BranchVerdictCase):
    def test_an_unlanded_branch_never_reaches_the_forge_cli(self) -> None:
        repo = self._repo_with_branch("one.py")

        with patch.object(branch_classification, "probe_host_cli", side_effect=AssertionError("forge probed")):
            assert branch_is_landed(str(repo), "feature") is False


class TestTheTargetIsResolvedNotHardcoded(BranchVerdictCase):
    def test_a_master_default_repo_is_measured_against_origin_master(self) -> None:
        repo = _clone_with_origin(self.root, default_branch="master")

        assert branch_verdict_report(str(repo), "master").target == "origin/master"

    def test_an_explicit_target_overrides_the_resolution(self) -> None:
        repo = self._repo_with_branch("one.py")

        assert branch_verdict_report(str(repo), "feature", target="main").target == "main"

    def test_branch_is_landed_measures_against_an_explicit_target(self) -> None:
        """#4423 — the cleanup guard shares its already-resolved default with the probe.

        Falsifiable both ways in one fixture: ``feature`` landed on ``origin/release`` and on
        nothing else, so a ``branch_is_landed`` that dropped the argument and re-resolved the
        repo default would answer False for the very ref the caller named.
        """
        repo = self._repo_with_branch("one.py")
        run_git(repo, "checkout", "-q", "-b", "release", "main")
        run_git(repo, "merge", "-q", "--squash", "feature")
        run_git(repo, "commit", "-q", "-m", "feat: one (#1)")
        run_git(repo, "push", "-q", "origin", "release")
        run_git(repo, "fetch", "-q", "origin")

        assert branch_is_landed(str(repo), "feature", "origin/release") is True
        assert branch_is_landed(str(repo), "feature") is False
