from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase

from teatree.core.cleanup import BranchClassification, BranchCommit
from teatree.core.gates.orphan_guard import BranchReport, BranchStatus, classify_branch, find_orphans_in_workspace
from teatree.core.models import Ticket, Worktree
from teatree.utils.run import CommandFailedError
from tests.teatree_core.cleanup._shared import _run_git

_patch_classify = patch("teatree.core.gates.orphan_guard.classify_branch_commits")
_patch_tree_match = patch("teatree.core.gates.orphan_guard._branch_tree_matches_squash")
_patch_open_pr = patch("teatree.core.gates.orphan_guard.find_open_pr")
_patch_git = patch("teatree.core.gates.orphan_guard.git")


def _classification(ahead: list[BranchCommit] | None = None) -> BranchClassification:
    return BranchClassification(genuinely_ahead=ahead or [])


def _commit(sha: str = "abc", subject: str = "feat: x") -> BranchCommit:
    return BranchCommit(sha=sha, subject=subject, is_merge=False)


class TestClassifyBranch(TestCase):
    @_patch_classify
    def test_synced_when_no_commits_ahead(self, mock_classify: MagicMock) -> None:
        mock_classify.return_value = _classification()
        report = classify_branch("/repo", "feature")
        assert report.status is BranchStatus.SYNCED
        assert report.ahead_count == 0
        assert not report.is_orphan

    @_patch_tree_match
    @_patch_classify
    def test_synced_when_tree_matches_squash_commit(
        self,
        mock_classify: MagicMock,
        mock_tree_match: MagicMock,
    ) -> None:
        mock_classify.return_value = _classification([_commit()])
        mock_tree_match.return_value = True
        report = classify_branch("/repo", "feature")
        assert report.status is BranchStatus.SYNCED

    @_patch_open_pr
    @_patch_tree_match
    @_patch_classify
    def test_open_pr_when_branch_has_open_pr(
        self,
        mock_classify: MagicMock,
        mock_tree_match: MagicMock,
        mock_open_pr: MagicMock,
    ) -> None:
        mock_classify.return_value = _classification([_commit()])
        mock_tree_match.return_value = False
        mock_open_pr.return_value = "https://github.com/org/repo/pull/42"
        report = classify_branch("/repo", "feature")
        assert report.status is BranchStatus.OPEN_PR
        assert report.open_pr_url == "https://github.com/org/repo/pull/42"
        assert not report.is_orphan

    @_patch_git
    @_patch_open_pr
    @_patch_tree_match
    @_patch_classify
    def test_pushed_orphan_when_remote_exists_but_no_open_pr(
        self,
        mock_classify: MagicMock,
        mock_tree_match: MagicMock,
        mock_open_pr: MagicMock,
        mock_git: MagicMock,
    ) -> None:
        mock_classify.return_value = _classification([_commit(), _commit("def", "feat: y")])
        mock_tree_match.return_value = False
        mock_open_pr.return_value = ""
        mock_git.run.return_value = "abc123\trefs/heads/feature"
        report = classify_branch("/repo", "feature")
        assert report.status is BranchStatus.PUSHED_ORPHAN
        assert report.ahead_count == 2
        assert report.is_orphan

    @_patch_git
    @_patch_open_pr
    @_patch_tree_match
    @_patch_classify
    def test_unpushed_orphan_when_no_remote_branch_exists(
        self,
        mock_classify: MagicMock,
        mock_tree_match: MagicMock,
        mock_open_pr: MagicMock,
        mock_git: MagicMock,
    ) -> None:
        mock_classify.return_value = _classification([_commit()])
        mock_tree_match.return_value = False
        mock_open_pr.return_value = ""
        mock_git.run.return_value = ""
        report = classify_branch("/repo", "feature")
        assert report.status is BranchStatus.UNPUSHED_ORPHAN
        assert report.is_orphan


class TestFindOrphansInWorkspace(TestCase):
    def _make_worktree(self, repo_path: str, branch: str) -> Worktree:
        ticket = Ticket.objects.create(
            issue_url=f"https://gitlab.com/org/{repo_path}/-/issues/{branch}",
            state=Ticket.State.IN_REVIEW,
        )
        return Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path=repo_path,
            branch=branch,
        )

    @patch("teatree.core.gates.orphan_guard.clone_root")
    @patch("teatree.core.gates.orphan_guard.classify_branch")
    def test_returns_only_orphans(
        self,
        mock_classify: MagicMock,
        mock_clone_root: MagicMock,
    ) -> None:
        fake_workspace = MagicMock()

        def _fake_div(_self: object, x: str) -> MagicMock:
            return MagicMock(spec=Path, is_dir=lambda: True, __str__=lambda _s: f"/ws/{x}")

        fake_workspace.__truediv__ = _fake_div
        mock_clone_root.return_value = fake_workspace

        self._make_worktree("org/alpha", "feat-1")
        self._make_worktree("org/beta", "feat-2")
        self._make_worktree("org/gamma", "feat-3")

        def classify(_repo: str, branch: str) -> BranchReport:
            statuses = {
                "feat-1": BranchStatus.SYNCED,
                "feat-2": BranchStatus.PUSHED_ORPHAN,
                "feat-3": BranchStatus.UNPUSHED_ORPHAN,
            }
            return BranchReport(repo=_repo, branch=branch, status=statuses[branch], ahead_count=1)

        mock_classify.side_effect = classify

        orphans = find_orphans_in_workspace()

        branches = [o.branch for o in orphans]
        assert "feat-1" not in branches
        assert "feat-2" in branches
        assert "feat-3" in branches
        assert len(orphans) == 2

    @patch("teatree.core.gates.orphan_guard.clone_root")
    @patch("teatree.core.gates.orphan_guard.classify_branch")
    def test_deduplicates_by_repo_and_branch(
        self,
        mock_classify: MagicMock,
        mock_clone_root: MagicMock,
    ) -> None:
        fake_workspace = MagicMock()

        def _fake_div(_self: object, x: str) -> MagicMock:
            return MagicMock(spec=Path, is_dir=lambda: True, __str__=lambda _s: f"/ws/{x}")

        fake_workspace.__truediv__ = _fake_div
        mock_clone_root.return_value = fake_workspace

        self._make_worktree("org/alpha", "feat-1")
        # Same repo+branch across tickets
        ticket2 = Ticket.objects.create(
            issue_url="https://gitlab.com/org/alpha/-/issues/200",
            state=Ticket.State.IN_REVIEW,
        )
        Worktree.objects.create(
            overlay="test",
            ticket=ticket2,
            repo_path="org/alpha",
            branch="feat-1",
        )

        mock_classify.return_value = BranchReport(
            repo="/ws/org/alpha",
            branch="feat-1",
            status=BranchStatus.PUSHED_ORPHAN,
            ahead_count=1,
        )

        orphans = find_orphans_in_workspace()

        assert len(orphans) == 1
        assert mock_classify.call_count == 1

    @patch("teatree.core.gates.orphan_guard.clone_root")
    @patch("teatree.core.gates.orphan_guard.classify_branch")
    def test_skips_worktree_whose_classification_fails_but_reports_the_rest(
        self,
        mock_classify: MagicMock,
        mock_clone_root: MagicMock,
    ) -> None:
        """#2937: one worktree's git failure must not crash the whole scan."""
        fake_workspace = MagicMock()

        def _fake_div(_self: object, x: str) -> MagicMock:
            return MagicMock(spec=Path, is_dir=lambda: True, __str__=lambda _s: f"/ws/{x}")

        fake_workspace.__truediv__ = _fake_div
        mock_clone_root.return_value = fake_workspace

        self._make_worktree("org/alpha", "feat-1")
        self._make_worktree("org/beta", "feat-2")

        def classify(repo: str, branch: str) -> BranchReport:
            if branch == "feat-1":
                raise CommandFailedError(
                    cmd=["git", "-C", repo, "log", branch, "--not", "origin/main"],
                    returncode=128,
                    stdout="",
                    stderr="fatal: cannot change to '/ws/org/alpha': No such file or directory",
                )
            return BranchReport(repo=repo, branch=branch, status=BranchStatus.PUSHED_ORPHAN, ahead_count=1)

        mock_classify.side_effect = classify

        orphans = find_orphans_in_workspace()

        branches = [o.branch for o in orphans]
        assert "feat-1" not in branches
        assert "feat-2" in branches
        assert len(orphans) == 1


class TestClassifyBranchRespectsRepoDefaultBranch:
    """Real-git integration: ``classify_branch`` must use the repo's actual default branch.

    ``classify_branch_commits`` defaults to ``target="origin/main"``; on a repo
    whose default branch is ``master`` (or anything else), one-commit-ahead-of-
    master was misclassified as SYNCED because the comparison was against the
    non-existent ``origin/main``.
    """

    @pytest.fixture(autouse=True)
    def _tmp_repo_with_master_default(self, tmp_path: Path) -> None:
        self.origin = tmp_path / "origin.git"
        _run_git("init", "-q", "--bare", "-b", "master", str(self.origin), cwd=tmp_path)

        self.clone = tmp_path / "clone"
        _run_git("clone", "-q", str(self.origin), str(self.clone), cwd=tmp_path)
        _run_git("config", "user.email", "t@t", cwd=self.clone)
        _run_git("config", "user.name", "t", cwd=self.clone)
        _run_git("commit", "--allow-empty", "-q", "-m", "initial on master", cwd=self.clone)
        _run_git("push", "-q", "origin", "master", cwd=self.clone)
        _run_git("checkout", "-q", "-b", "feature-branch", cwd=self.clone)
        _run_git("commit", "--allow-empty", "-q", "-m", "feat: new thing on feature", cwd=self.clone)

    def test_one_commit_ahead_of_master_is_not_classified_as_synced(self) -> None:
        report = classify_branch(str(self.clone), "feature-branch")
        assert report.status is not BranchStatus.SYNCED, (
            "Branch with one unpushed commit on top of origin/master must not "
            "be classified as SYNCED (origin/main was hardcoded)"
        )
        assert report.ahead_count == 1
        assert report.is_orphan

    def test_falls_back_to_origin_main_when_default_branch_undetectable(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Fallback path — ``git.default_branch`` raises, classifier falls back to ``origin/main``.

        When the repo has no ``origin/HEAD`` and no known fallback name on the
        remote, ``classify_branch`` still attempts ``origin/main``. On this
        fixture that ref genuinely does not exist either (the remote's only
        branch is ``master``), so the underlying ``git log ... --not
        origin/main`` fails for real — and per #2937 that failure must
        propagate (fail loud), never silently degrade to a possibly-wrong
        report.
        """
        from teatree.core.gates import orphan_guard as og  # noqa: PLC0415

        msg = "could not detect default branch"

        def _raise(repo: str) -> str:
            raise RuntimeError(msg)

        monkeypatch.setattr(og.git, "default_branch", _raise)
        with pytest.raises(CommandFailedError, match="origin/main"):
            classify_branch(str(self.clone), "feature-branch")


class TestClassifyBranchFailsLoudOnGitFailure:
    """#2937.

    An invalid ``repo`` filesystem path must fail loud, never silently
    misclassify a genuinely-ahead branch as SYNCED.

    ``t3 <overlay> pr ensure-pr --repo <owner/repo-slug>`` passes a forge
    slug (``owner/repo``) where a filesystem path is expected. ``git -C
    <bad-path> log ...`` then fails, and the classifier used to swallow that
    failure as an empty (legitimately-synced-looking) result.
    """

    def test_nonexistent_repo_path_raises_instead_of_reporting_synced(self, tmp_path: Path) -> None:
        # Never created — mimics passing a forge slug like "owner/repo"
        # instead of a real checkout path.
        bad_repo = str(tmp_path / "owner" / "repo")
        with pytest.raises(CommandFailedError):
            classify_branch(bad_repo, "feature-branch")
