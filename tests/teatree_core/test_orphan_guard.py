from pathlib import Path
from unittest.mock import MagicMock, patch

from django.test import TestCase

from teatree.core.cleanup import BranchClassification, BranchCommit
from teatree.core.models import Ticket, Worktree
from teatree.core.orphan_guard import (
    BranchReport,
    BranchStatus,
    classify_branch,
    find_orphans_in_workspace,
)

_patch_classify = patch("teatree.core.orphan_guard.classify_branch_commits")
_patch_tree_match = patch("teatree.core.orphan_guard._branch_tree_matches_squash")
_patch_open_pr = patch("teatree.core.orphan_guard.find_open_pr")
_patch_git = patch("teatree.core.orphan_guard.git")


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

    @patch("teatree.core.orphan_guard.load_config")
    @patch("teatree.core.orphan_guard.classify_branch")
    def test_returns_only_orphans(
        self,
        mock_classify: MagicMock,
        mock_config: MagicMock,
    ) -> None:
        fake_workspace = MagicMock()

        def _fake_div(_self: object, x: str) -> MagicMock:
            return MagicMock(spec=Path, is_dir=lambda: True, __str__=lambda _s: f"/ws/{x}")

        fake_workspace.__truediv__ = _fake_div
        mock_config.return_value.user.workspace_dir = fake_workspace

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

    @patch("teatree.core.orphan_guard.load_config")
    @patch("teatree.core.orphan_guard.classify_branch")
    def test_deduplicates_by_repo_and_branch(
        self,
        mock_classify: MagicMock,
        mock_config: MagicMock,
    ) -> None:
        fake_workspace = MagicMock()

        def _fake_div(_self: object, x: str) -> MagicMock:
            return MagicMock(spec=Path, is_dir=lambda: True, __str__=lambda _s: f"/ws/{x}")

        fake_workspace.__truediv__ = _fake_div
        mock_config.return_value.user.workspace_dir = fake_workspace

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
