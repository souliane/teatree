import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import pytest
from django.test import TestCase

from teatree.core.cleanup import BranchClassification, BranchCommit, classify_branch_commits, cleanup_worktree
from teatree.core.models import Ticket, Worktree
from teatree.core.worktree_recovery import _has_unpushed_commits, capture_recovery_artifact
from teatree.utils.run import CommandFailedError

_patch_config = patch("teatree.core.cleanup.load_config")
_patch_git = patch("teatree.core.cleanup.git")
_patch_overlay = patch("teatree.core.cleanup.get_overlay")
_patch_classify = patch("teatree.core.cleanup.classify_branch_commits")


def _no_unpushed(mock_git: MagicMock) -> None:
    """Default the #706 data-loss guard helper to "nothing unpushed".

    Tests exercising unrelated cleanup behaviour share the wholesale ``git``
    mock; without this the guard sees a truthy ``MagicMock`` and refuses
    spuriously. Tests that target the guard override the return value after
    calling this.
    """
    mock_git.commits_absent_from_all_remotes.return_value = []


def _mock_workspace(mock_config: MagicMock) -> None:
    mock_config.return_value.user.workspace_dir.__truediv__ = lambda self, x: MagicMock(is_dir=lambda: True)


class TestCleanupWorktree(TestCase):
    def _make_worktree(self, *, wt_path: str = "", db_name: str = "") -> Worktree:
        ticket = Ticket.objects.create(
            issue_url="https://gitlab.com/org/repo/-/issues/99",
            state=Ticket.State.IN_REVIEW,
        )
        extra = {"worktree_path": wt_path} if wt_path else {}
        return Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="org/repo",
            branch="fix-99",
            db_name=db_name,
            extra=extra,
        )

    @_patch_overlay
    @_patch_git
    @_patch_config
    def test_removes_git_worktree_and_branch(
        self,
        mock_config: MagicMock,
        mock_git: MagicMock,
        mock_overlay: MagicMock,
    ) -> None:
        _mock_workspace(mock_config)
        _no_unpushed(mock_git)
        mock_overlay.return_value.get_cleanup_steps.return_value = []
        mock_git.status_porcelain.return_value = ""
        mock_git.unsynced_commits.return_value = []

        wt = self._make_worktree(wt_path="/tmp/wt/org/repo")
        wt_id = wt.pk
        label = cleanup_worktree(wt)

        mock_git.worktree_remove.assert_called_once()
        mock_git.branch_delete.assert_called_once()
        assert "org/repo" in label
        assert "fix-99" in label
        assert not Worktree.objects.filter(pk=wt_id).exists()

    @_patch_overlay
    @_patch_git
    @_patch_config
    def test_drops_database_when_db_name_set(
        self,
        mock_config: MagicMock,
        mock_git: MagicMock,
        mock_overlay: MagicMock,
    ) -> None:
        _mock_workspace(mock_config)
        _no_unpushed(mock_git)
        mock_overlay.return_value.get_cleanup_steps.return_value = []

        wt = self._make_worktree(db_name="wt_99")

        with patch("teatree.core.cleanup.drop_db") as mock_drop:
            cleanup_worktree(wt)
            mock_drop.assert_called_once_with("wt_99", user="")

    @_patch_overlay
    @_patch_git
    @_patch_config
    def test_runs_overlay_cleanup_steps(
        self,
        mock_config: MagicMock,
        mock_git: MagicMock,
        mock_overlay: MagicMock,
    ) -> None:
        _mock_workspace(mock_config)
        _no_unpushed(mock_git)
        step_fn = MagicMock()
        mock_overlay.return_value.get_cleanup_steps.return_value = [MagicMock(callable=step_fn)]

        wt = self._make_worktree()
        cleanup_worktree(wt)

        step_fn.assert_called_once()

    @_patch_overlay
    @_patch_git
    @_patch_config
    def test_skips_pass_remove_when_setting_disabled(
        self,
        mock_config: MagicMock,
        mock_git: MagicMock,
        mock_overlay: MagicMock,
    ) -> None:
        _mock_workspace(mock_config)
        _no_unpushed(mock_git)
        mock_overlay.return_value.get_cleanup_steps.return_value = []
        mock_overlay.return_value.config.teardown_removes_pass_entries = False

        wt = self._make_worktree(wt_path="/tmp/wt/org/repo")
        with patch("teatree.core.cleanup.remove_postgres_pass_entry") as mock_remove:
            cleanup_worktree(wt)
        mock_remove.assert_not_called()

    @_patch_overlay
    @_patch_git
    @_patch_config
    def test_removes_pass_entry_when_setting_enabled(
        self,
        mock_config: MagicMock,
        mock_git: MagicMock,
        mock_overlay: MagicMock,
    ) -> None:
        _mock_workspace(mock_config)
        _no_unpushed(mock_git)
        mock_overlay.return_value.get_cleanup_steps.return_value = []
        mock_overlay.return_value.config.teardown_removes_pass_entries = True

        wt = self._make_worktree(wt_path="/tmp/wt/org/repo")
        ticket_number = wt.ticket.ticket_number
        with patch("teatree.core.cleanup.remove_postgres_pass_entry") as mock_remove:
            cleanup_worktree(wt)
        mock_remove.assert_called_once_with(ticket_number)

    @_patch_overlay
    @_patch_git
    @_patch_config
    def test_deletes_worktree_record(
        self,
        mock_config: MagicMock,
        mock_git: MagicMock,
        mock_overlay: MagicMock,
    ) -> None:
        _mock_workspace(mock_config)
        _no_unpushed(mock_git)
        mock_overlay.return_value.get_cleanup_steps.return_value = []

        wt = self._make_worktree()
        wt_id = wt.pk
        cleanup_worktree(wt)

        assert not Worktree.objects.filter(pk=wt_id).exists()

    @_patch_classify
    @_patch_overlay
    @_patch_git
    @_patch_config
    def test_raises_when_genuinely_ahead_commits_present(
        self,
        mock_config: MagicMock,
        mock_git: MagicMock,
        mock_overlay: MagicMock,
        mock_classify: MagicMock,
    ) -> None:
        _mock_workspace(mock_config)
        _no_unpushed(mock_git)
        mock_overlay.return_value.get_cleanup_steps.return_value = []
        mock_git.status_porcelain.return_value = ""
        mock_git.unsynced_commits.return_value = ["abc123 chore: cve fix"]
        mock_classify.return_value = BranchClassification(
            genuinely_ahead=[BranchCommit(sha="abc123", subject="chore: cve fix", is_merge=False)]
        )

        wt = self._make_worktree(wt_path="/tmp/wt/org/repo")
        with (
            patch("teatree.core.cleanup._pr_merge_commit_sha", return_value=""),
            pytest.raises(RuntimeError, match="unsynced commit"),
        ):
            cleanup_worktree(wt)

        mock_git.worktree_remove.assert_not_called()
        mock_git.branch_delete.assert_not_called()

    @_patch_classify
    @_patch_overlay
    @_patch_git
    @_patch_config
    def test_cleans_when_genuinely_ahead_tree_matches_pr_squash_commit(
        self,
        mock_config: MagicMock,
        mock_git: MagicMock,
        mock_overlay: MagicMock,
        mock_classify: MagicMock,
    ) -> None:
        """Post-merge follow-ups tree-equal to PR squash are safe to clean.

        Genuinely-ahead commits whose cumulative tree matches the PR's squash
        commit are still safe to remove because their content is already in
        main. Reproduces the common case where an agent pushes retro/docs
        commits AFTER the PR was squash-merged; those commits' net effect
        is already captured by the squash tree.
        """
        _mock_workspace(mock_config)
        _no_unpushed(mock_git)
        mock_overlay.return_value.get_cleanup_steps.return_value = []
        mock_git.status_porcelain.return_value = ""
        mock_git.unsynced_commits.return_value = ["abc123 retro: post-merge docs"]
        mock_classify.return_value = BranchClassification(
            genuinely_ahead=[BranchCommit(sha="abc123", subject="retro: post-merge docs", is_merge=False)]
        )
        mock_git.check.return_value = True  # git diff --quiet returns 0 → tree-equal

        wt = self._make_worktree(wt_path="/tmp/wt/org/repo")
        with patch("teatree.core.cleanup._pr_merge_commit_sha", return_value="squash123"):
            cleanup_worktree(wt)

        mock_git.worktree_remove.assert_called_once()
        mock_git.branch_delete.assert_called_once()

    @_patch_classify
    @_patch_overlay
    @_patch_git
    @_patch_config
    def test_raises_when_genuinely_ahead_tree_differs_from_pr_squash_commit(
        self,
        mock_config: MagicMock,
        mock_git: MagicMock,
        mock_overlay: MagicMock,
        mock_classify: MagicMock,
    ) -> None:
        """Genuinely ahead commits whose tree differs from the squash carry real work."""
        _mock_workspace(mock_config)
        _no_unpushed(mock_git)
        mock_overlay.return_value.get_cleanup_steps.return_value = []
        mock_git.status_porcelain.return_value = ""
        mock_git.unsynced_commits.return_value = ["abc123 feat: new work"]
        mock_classify.return_value = BranchClassification(
            genuinely_ahead=[BranchCommit(sha="abc123", subject="feat: new work", is_merge=False)]
        )
        mock_git.check.return_value = False  # git diff --quiet returns 1 → tree differs

        wt = self._make_worktree(wt_path="/tmp/wt/org/repo")
        with (
            patch("teatree.core.cleanup._pr_merge_commit_sha", return_value="squash123"),
            pytest.raises(RuntimeError, match="unsynced commit"),
        ):
            cleanup_worktree(wt)

    @_patch_classify
    @_patch_overlay
    @_patch_git
    @_patch_config
    def test_cleans_when_only_squash_merged_and_merge_commits(
        self,
        mock_config: MagicMock,
        mock_git: MagicMock,
        mock_overlay: MagicMock,
        mock_classify: MagicMock,
    ) -> None:
        """Branches whose only "unsynced" commits are squash-merged or merge commits are safe to clean."""
        _mock_workspace(mock_config)
        _no_unpushed(mock_git)
        mock_overlay.return_value.get_cleanup_steps.return_value = []
        mock_git.status_porcelain.return_value = ""
        mock_git.unsynced_commits.return_value = ["abc123 feat: squashed on main"]
        mock_classify.return_value = BranchClassification(
            squash_merged=[BranchCommit(sha="abc123", subject="feat: squashed on main", is_merge=False)],
            merge_commits=[BranchCommit(sha="mrg001", subject="Merge branch 'main'", is_merge=True)],
            genuinely_ahead=[],
        )

        wt = self._make_worktree(wt_path="/tmp/wt/org/repo")
        cleanup_worktree(wt)

        mock_git.worktree_remove.assert_called_once()
        mock_git.branch_delete.assert_called_once()

    @_patch_overlay
    @_patch_git
    @_patch_config
    def test_force_bypasses_unsynced_check(
        self,
        mock_config: MagicMock,
        mock_git: MagicMock,
        mock_overlay: MagicMock,
    ) -> None:
        _mock_workspace(mock_config)
        _no_unpushed(mock_git)
        mock_overlay.return_value.get_cleanup_steps.return_value = []
        mock_git.status_porcelain.return_value = ""
        mock_git.unsynced_commits.return_value = ["abc123 chore: cve fix"]

        wt = self._make_worktree(wt_path="/tmp/wt/org/repo")
        cleanup_worktree(wt, force=True)

        mock_git.worktree_remove.assert_called_once()
        mock_git.branch_delete.assert_called_once()

    @_patch_overlay
    @_patch_git
    @_patch_config
    def test_raises_when_commits_absent_from_all_remotes(
        self,
        mock_config: MagicMock,
        mock_git: MagicMock,
        mock_overlay: MagicMock,
    ) -> None:
        """#706 data-loss guard — branch with commits on no remote blocks teardown."""
        _mock_workspace(mock_config)
        _no_unpushed(mock_git)
        mock_overlay.return_value.get_cleanup_steps.return_value = []
        mock_git.status_porcelain.return_value = ""
        mock_git.commits_absent_from_all_remotes.return_value = [
            "abc1234 feat: never pushed",
            "def5678 fix: also local",
        ]

        wt = self._make_worktree(wt_path="/tmp/wt/org/repo")
        with pytest.raises(RuntimeError, match=r"on NO remote \(data loss\)"):
            cleanup_worktree(wt)

        mock_git.worktree_remove.assert_not_called()
        mock_git.branch_delete.assert_not_called()

    @_patch_overlay
    @_patch_git
    @_patch_config
    def test_unpushed_guard_message_truncates_sha_preview(
        self,
        mock_config: MagicMock,
        mock_git: MagicMock,
        mock_overlay: MagicMock,
    ) -> None:
        """More than the preview limit of unpushed commits is summarised with an ellipsis."""
        _mock_workspace(mock_config)
        _no_unpushed(mock_git)
        mock_overlay.return_value.get_cleanup_steps.return_value = []
        mock_git.status_porcelain.return_value = ""
        mock_git.commits_absent_from_all_remotes.return_value = [f"sha{i} commit {i}" for i in range(5)]

        wt = self._make_worktree(wt_path="/tmp/wt/org/repo")
        with pytest.raises(RuntimeError, match=r"5 commit\(s\) on NO remote.*…") as excinfo:
            cleanup_worktree(wt)
        assert "sha0" in str(excinfo.value)
        assert "sha4" not in str(excinfo.value)

    @_patch_overlay
    @_patch_git
    @_patch_config
    def test_force_bypasses_unpushed_guard(
        self,
        mock_config: MagicMock,
        mock_git: MagicMock,
        mock_overlay: MagicMock,
    ) -> None:
        """An explicit force override discards even commits on no remote."""
        _mock_workspace(mock_config)
        _no_unpushed(mock_git)
        mock_overlay.return_value.get_cleanup_steps.return_value = []
        mock_git.status_porcelain.return_value = ""
        mock_git.commits_absent_from_all_remotes.return_value = ["abc123 feat: unpushed"]

        wt = self._make_worktree(wt_path="/tmp/wt/org/repo")
        cleanup_worktree(wt, force=True)

        mock_git.worktree_remove.assert_called_once()
        mock_git.commits_absent_from_all_remotes.assert_not_called()

    @_patch_classify
    @_patch_overlay
    @_patch_git
    @_patch_config
    def test_strict_hygiene_refuses_pushed_but_unmerged_branch(
        self,
        mock_config: MagicMock,
        mock_git: MagicMock,
        mock_overlay: MagicMock,
        mock_classify: MagicMock,
    ) -> None:
        """Pushed-but-unmerged branch is refused under strict hygiene (default).

        The origin/main hygiene gate still blocks it — the sync-backend /
        clean-all contract is unchanged.
        """
        _mock_workspace(mock_config)
        _no_unpushed(mock_git)
        mock_overlay.return_value.get_cleanup_steps.return_value = []
        mock_git.status_porcelain.return_value = ""
        mock_git.commits_absent_from_all_remotes.return_value = []  # pushed → data-loss guard passes
        mock_git.unsynced_commits.return_value = ["abc123 feat: pushed not merged"]
        mock_classify.return_value = BranchClassification(
            genuinely_ahead=[BranchCommit(sha="abc123", subject="feat: pushed not merged", is_merge=False)]
        )

        wt = self._make_worktree(wt_path="/tmp/wt/org/repo")
        with (
            patch("teatree.core.cleanup._pr_merge_commit_sha", return_value=""),
            pytest.raises(RuntimeError, match="unsynced commit"),
        ):
            cleanup_worktree(wt, strict_hygiene=True)
        mock_git.worktree_remove.assert_not_called()

    @_patch_classify
    @_patch_overlay
    @_patch_git
    @_patch_config
    def test_non_strict_hygiene_allows_pushed_but_unmerged_branch(
        self,
        mock_config: MagicMock,
        mock_git: MagicMock,
        mock_overlay: MagicMock,
        mock_classify: MagicMock,
    ) -> None:
        """Pushed-but-unmerged branch is allowed when strict hygiene is off.

        This is the automated FSM teardown contract — a branch pushed to its
        own remote ref passes; only the data-loss guard still applies.
        """
        _mock_workspace(mock_config)
        _no_unpushed(mock_git)
        mock_overlay.return_value.get_cleanup_steps.return_value = []
        mock_git.status_porcelain.return_value = ""
        mock_git.commits_absent_from_all_remotes.return_value = []  # pushed
        mock_git.unsynced_commits.return_value = ["abc123 feat: pushed not merged"]
        mock_classify.return_value = BranchClassification(
            genuinely_ahead=[BranchCommit(sha="abc123", subject="feat: pushed not merged", is_merge=False)]
        )

        wt = self._make_worktree(wt_path="/tmp/wt/org/repo")
        cleanup_worktree(wt, strict_hygiene=False)
        mock_git.worktree_remove.assert_called_once()

    @_patch_overlay
    @_patch_git
    @_patch_config
    def test_releases_redis_slot_when_last_worktree_removed(
        self,
        mock_config: MagicMock,
        mock_git: MagicMock,
        mock_overlay: MagicMock,
    ) -> None:
        _mock_workspace(mock_config)
        _no_unpushed(mock_git)
        mock_overlay.return_value.get_cleanup_steps.return_value = []
        mock_git.status_porcelain.return_value = ""
        mock_git.unsynced_commits.return_value = []

        wt = self._make_worktree(wt_path="/tmp/wt/org/repo")
        ticket = wt.ticket
        ticket.redis_db_index = 3
        ticket.save()

        with patch("teatree.utils.redis_container.flushdb") as mock_flush:
            cleanup_worktree(wt)

        mock_flush.assert_called_once_with(3, db_count=ANY)
        ticket.refresh_from_db()
        assert ticket.redis_db_index is None

    @_patch_overlay
    @_patch_git
    @_patch_config
    def test_keeps_redis_slot_when_other_worktrees_remain(
        self,
        mock_config: MagicMock,
        mock_git: MagicMock,
        mock_overlay: MagicMock,
    ) -> None:
        _mock_workspace(mock_config)
        _no_unpushed(mock_git)
        mock_overlay.return_value.get_cleanup_steps.return_value = []
        mock_git.status_porcelain.return_value = ""
        mock_git.unsynced_commits.return_value = []

        wt = self._make_worktree(wt_path="/tmp/wt/org/repo")
        ticket = wt.ticket
        ticket.redis_db_index = 4
        ticket.save()

        # Sibling worktree keeps ticket alive
        Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="org/other",
            branch="fix-99",
        )

        with patch("teatree.utils.redis_container.flushdb") as mock_flush:
            cleanup_worktree(wt)

        mock_flush.assert_not_called()
        ticket.refresh_from_db()
        assert ticket.redis_db_index == 4

    @_patch_overlay
    @_patch_git
    @_patch_config
    def test_proceeds_normally_when_fully_synced(
        self,
        mock_config: MagicMock,
        mock_git: MagicMock,
        mock_overlay: MagicMock,
    ) -> None:
        _mock_workspace(mock_config)
        _no_unpushed(mock_git)
        mock_overlay.return_value.get_cleanup_steps.return_value = []
        mock_git.status_porcelain.return_value = ""
        mock_git.unsynced_commits.return_value = []

        wt = self._make_worktree(wt_path="/tmp/wt/org/repo")
        cleanup_worktree(wt)

        mock_git.worktree_remove.assert_called_once()
        mock_git.branch_delete.assert_called_once()


class TestClassifyBranchCommits(TestCase):
    """``classify_branch_commits`` sorts branch-local commits into three buckets.

    The classifier is the foundation for squash-merge-aware cleanup: it lets
    the caller distinguish content already on the default branch (under a new
    SHA, via squash-merge) from work that still needs pushing.
    """

    @patch("teatree.core.cleanup.git.run")
    def test_empty_when_no_unsynced_commits(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = ["", ""]  # unsynced log empty, target log empty
        result = classify_branch_commits("/repo", "feature")
        assert result == BranchClassification(squash_merged=[], merge_commits=[], genuinely_ahead=[])

    @patch("teatree.core.cleanup.git.run")
    def test_subject_match_with_pr_suffix_marks_squash_merged(self, mock_run: MagicMock) -> None:
        branch_log = "abc123\x00parent1\x00fix(ui): button alignment"
        target_log = "fix(ui): button alignment (#42)\nfeat(core): unrelated"
        mock_run.side_effect = [branch_log, target_log]

        result = classify_branch_commits("/repo", "feature")

        assert result.squash_merged == [BranchCommit(sha="abc123", subject="fix(ui): button alignment", is_merge=False)]
        assert result.genuinely_ahead == []

    @patch("teatree.core.cleanup.git.run")
    def test_strips_type_prefix_for_relax_to_feat_rewrite(self, mock_run: MagicMock) -> None:
        """Branch has ``relax: X (#140)``; main has ``feat(fsm): X (#140) (#368)`` — same content, different prefix."""
        branch_log = "def456\x00parent1\x00relax: transition-driven workflow (#140)"
        target_log = "feat(fsm): transition-driven workflow (#140) (#368)"
        mock_run.side_effect = [branch_log, target_log]

        result = classify_branch_commits("/repo", "feature")

        assert len(result.squash_merged) == 1
        assert result.squash_merged[0].sha == "def456"
        assert result.genuinely_ahead == []

    @patch("teatree.core.cleanup.git.run")
    def test_merge_commit_detected_via_multiple_parents(self, mock_run: MagicMock) -> None:
        branch_log = "mrg001\x00parent1 parent2\x00Merge branch 'main' into feature"
        target_log = ""
        mock_run.side_effect = [branch_log, target_log]

        result = classify_branch_commits("/repo", "feature")

        assert len(result.merge_commits) == 1
        assert result.merge_commits[0].is_merge is True
        assert result.genuinely_ahead == []
        assert result.squash_merged == []

    @patch("teatree.core.cleanup.git.run")
    def test_genuinely_ahead_when_no_subject_match(self, mock_run: MagicMock) -> None:
        branch_log = "new001\x00parent1\x00fix(hooks): strip trailing whitespace"
        target_log = "chore(deps): bump pytest\nfeat(config): add t3.mode"
        mock_run.side_effect = [branch_log, target_log]

        result = classify_branch_commits("/repo", "feature")

        assert result.squash_merged == []
        assert len(result.genuinely_ahead) == 1
        assert result.genuinely_ahead[0].sha == "new001"

    @patch("teatree.core.cleanup.git.run")
    def test_mixed_buckets(self, mock_run: MagicMock) -> None:
        branch_log = (
            "sha1\x00p1\x00feat(config): add setting\n"
            "sha2\x00p1 p2\x00Merge branch 'main'\n"
            "sha3\x00p1\x00fix(hooks): strip whitespace"
        )
        target_log = "feat(config): add setting (#100)\nchore: unrelated"
        mock_run.side_effect = [branch_log, target_log]

        result = classify_branch_commits("/repo", "feature")

        assert [c.sha for c in result.squash_merged] == ["sha1"]
        assert [c.sha for c in result.merge_commits] == ["sha2"]
        assert [c.sha for c in result.genuinely_ahead] == ["sha3"]

    @patch("teatree.core.cleanup.git.run")
    def test_unsynced_fully_merged_via_squash_returns_empty_genuinely_ahead(self, mock_run: MagicMock) -> None:
        """Every unsynced commit has a subject match on target → branch is safe to clean."""
        branch_log = "sha1\x00p1\x00feat(config): generic per-overlay override\nsha2\x00p1\x00fix: trailing whitespace"
        target_log = "feat(config): generic per-overlay override (#375)\nfix: trailing whitespace (#200)"
        mock_run.side_effect = [branch_log, target_log]

        result = classify_branch_commits("/repo", "feature")

        assert result.genuinely_ahead == []
        assert len(result.squash_merged) == 2

    @patch("teatree.core.cleanup.git.run")
    def test_release_note_suffix_on_target_matches_plain_local_subject(self, mock_run: MagicMock) -> None:
        """Regression for #387 — target carries ``[flag] (url) (#NNN)``, local has only the plain subject."""
        branch_log = "sha1\x00p1\x00fix(ship,workspace): pre-push main merge + t3 pr create over raw gh/glab"
        target_log = (
            "fix(ship,workspace): pre-push main merge + t3 pr create over raw gh/glab "
            "[none] (https://github.com/souliane/teatree/issues/379) (#386)"
        )
        mock_run.side_effect = [branch_log, target_log]

        result = classify_branch_commits("/repo", "feature")

        assert [c.sha for c in result.squash_merged] == ["sha1"]
        assert result.genuinely_ahead == []

    @patch("teatree.core.cleanup.git.run")
    def test_release_note_suffix_on_both_sides_matches(self, mock_run: MagicMock) -> None:
        """Both local and target carry the release-note suffix — canonicalization must strip from both."""
        branch_log = (
            "sha1\x00p1\x00relax(workspace): squash-merge-aware cleanup "
            "[none] (https://github.com/souliane/teatree/issues/379)"
        )
        target_log = (
            "relax(workspace): squash-merge-aware cleanup "
            "[none] (https://github.com/souliane/teatree/issues/379) (#384)"
        )
        mock_run.side_effect = [branch_log, target_log]

        result = classify_branch_commits("/repo", "feature")

        assert [c.sha for c in result.squash_merged] == ["sha1"]
        assert result.genuinely_ahead == []

    @patch("teatree.core.cleanup.git.run")
    def test_plain_subjects_without_release_note_suffix_still_match(self, mock_run: MagicMock) -> None:
        """Fallback case — neither title has a release-note suffix (e.g. ``chore:`` without ticket)."""
        branch_log = "sha1\x00p1\x00chore(prek): move pip-audit to manual stage"
        target_log = "chore(prek): move pip-audit to manual stage (#383)"
        mock_run.side_effect = [branch_log, target_log]

        result = classify_branch_commits("/repo", "feature")

        assert [c.sha for c in result.squash_merged] == ["sha1"]
        assert result.genuinely_ahead == []


_GIT = shutil.which("git") or "/usr/bin/git"
_RM = shutil.which("rm") or "/bin/rm"


def _clean_env() -> dict[str, str]:
    """Env with all ``GIT_*`` stripped (AGENTS.md § Test-Writing Doctrine, #288).

    The suite can run from the inline pre-commit ``pytest`` hook, where the
    outer ``git commit`` exports ``GIT_DIR``/``GIT_INDEX_FILE``/``GIT_WORK_TREE``.
    Inherited, they hijack the tmp-repo ``git`` calls so a test that passes
    standalone corrupts the real repo under ``git commit``.
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _run_git(*args: str, cwd: Path) -> None:
    subprocess.run([_GIT, "-C", str(cwd), *args], check=True, capture_output=True, env=_clean_env())


class TestCleanupWorktreeRemovesOnDiskWorktree(TestCase):
    """Real-git integration: cleanup must remove the on-disk worktree even when extras lack ``worktree_path``.

    Reproduces #460 — ``Worktree.extra['worktree_path']`` can be missing when
    a row exists without successful provisioning recording the path. The
    canonical layout (``workspace/<branch>/<repo-leaf>``) is enough to find
    and remove the on-disk worktree.
    """

    @pytest.fixture(autouse=True)
    def _tmp_workspace(self, tmp_path: Path) -> None:
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir()
        self.repo_main = self.workspace / "myrepo"
        self.repo_main.mkdir()
        _run_git("init", "-q", "-b", "main", cwd=self.repo_main)
        _run_git("config", "user.email", "t@t", cwd=self.repo_main)
        _run_git("config", "user.name", "t", cwd=self.repo_main)
        _run_git("commit", "--allow-empty", "-q", "-m", "initial", cwd=self.repo_main)
        self.branch = "ac-myrepo-99-x"
        self.wt_path = self.workspace / self.branch / "myrepo"
        _run_git("worktree", "add", "-q", "-b", self.branch, str(self.wt_path), cwd=self.repo_main)

    def _make_worktree(self, *, with_extras: bool) -> Worktree:
        ticket = Ticket.objects.create(
            issue_url="https://example.com/issues/99",
            state=Ticket.State.IN_REVIEW,
        )
        extras = {"worktree_path": str(self.wt_path)} if with_extras else {}
        return Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="myrepo",
            branch=self.branch,
            extra=extras,
        )

    def _cleanup(self, worktree: Worktree) -> str:
        with (
            patch("teatree.core.cleanup.load_config") as mock_config,
            patch("teatree.core.cleanup.get_overlay") as mock_overlay,
        ):
            mock_config.return_value.user.workspace_dir = self.workspace
            mock_overlay.return_value.get_cleanup_steps.return_value = []
            return cleanup_worktree(worktree, force=True)

    def _registered_worktrees(self) -> str:
        return subprocess.run(
            [_GIT, "-C", str(self.repo_main), "worktree", "list"],
            check=True,
            capture_output=True,
            text=True,
            env=_clean_env(),
        ).stdout

    def test_removes_worktree_when_extras_have_path(self) -> None:
        """Baseline — the existing happy path also exercises real git."""
        wt = self._make_worktree(with_extras=True)
        self._cleanup(wt)
        assert not self.wt_path.exists()
        assert str(self.wt_path) not in self._registered_worktrees()

    def test_removes_worktree_when_extras_missing_path(self) -> None:
        """#460 — without ``worktree_path`` in extras the dir + registry entry must still be removed."""
        wt = self._make_worktree(with_extras=False)
        self._cleanup(wt)
        assert not self.wt_path.exists(), "worktree directory survived cleanup"
        assert str(self.wt_path) not in self._registered_worktrees(), "git worktree registry entry survived"

    def test_surfaces_failure_in_label_when_git_remove_fails(self) -> None:
        """When the git ops can't complete (e.g., source repo missing), the label must report it."""
        wt = self._make_worktree(with_extras=True)
        # Wipe the source repo so git operations fail
        subprocess.run([_RM, "-rf", str(self.repo_main)], check=True, env=_clean_env())
        label = self._cleanup(wt)
        assert "errors" in label.lower() or "Cleaned: myrepo" in label
        # Worktree row deleted regardless so the operator can retry without DB cruft
        assert not Worktree.objects.filter(pk=wt.pk).exists()


class TestCleanupWorktreeNamespacedClone(TestCase):
    @pytest.fixture(autouse=True)
    def _tmp_workspace(self, tmp_path: Path) -> None:
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir()
        self.repo_main = self.workspace / "souliane" / "teatree"
        self.repo_main.mkdir(parents=True)
        _run_git("init", "-q", "-b", "main", cwd=self.repo_main)
        _run_git("config", "user.email", "t@t", cwd=self.repo_main)
        _run_git("config", "user.name", "t", cwd=self.repo_main)
        _run_git("commit", "--allow-empty", "-q", "-m", "initial", cwd=self.repo_main)
        self.branch = "ac-teatree-491-x"
        self.wt_path = self.workspace / self.branch / "teatree"
        _run_git("worktree", "add", "-q", "-b", self.branch, str(self.wt_path), cwd=self.repo_main)

    def test_resolves_namespaced_clone_via_extra(self) -> None:
        ticket = Ticket.objects.create(
            issue_url="https://example.com/issues/491",
            state=Ticket.State.IN_REVIEW,
        )
        wt = Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="teatree",
            branch=self.branch,
            extra={"worktree_path": str(self.wt_path), "clone_path": str(self.repo_main)},
        )

        with (
            patch("teatree.core.cleanup.load_config") as mock_config,
            patch("teatree.core.cleanup.get_overlay") as mock_overlay,
        ):
            mock_config.return_value.user.workspace_dir = self.workspace
            mock_overlay.return_value.get_cleanup_steps.return_value = []
            label = cleanup_worktree(wt, force=True)

        assert not self.wt_path.exists()
        registry = subprocess.run(
            [_GIT, "-C", str(self.repo_main), "worktree", "list"],
            check=True,
            capture_output=True,
            text=True,
            env=_clean_env(),
        ).stdout
        assert str(self.wt_path) not in registry
        assert "errors" not in label.lower()


def _recovery_dirs(temp_root: Path) -> list[Path]:
    return sorted(p for p in temp_root.iterdir() if p.is_dir() and p.name.startswith("t3-recover-"))


class TestCleanupWorktreeRecoversDirtyOrUnpushedWork(TestCase):
    """#835 — pruning a dirty/unpushed worktree captures recovery first.

    A worktree with uncommitted changes OR unpushed commits must first capture
    a self-contained, restorable recovery artifact (a git
    bundle of the branch + a working-tree diff) under the system temp dir, then
    remove the worktree. A clean, merged worktree must still hard-delete with no
    artifact written (preserve-behavior).
    """

    @pytest.fixture(autouse=True)
    def _tmp_workspace(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir()
        self.temp_root = tmp_path / "systmp"
        self.temp_root.mkdir()
        monkeypatch.setattr(
            "teatree.core.worktree_recovery.tempfile.gettempdir",
            lambda: str(self.temp_root),
        )

        # A bare "remote" so origin/main exists and branch commits can be
        # classified as pushed-or-not.
        self.remote = tmp_path / "remote.git"
        _run_git("init", "-q", "--bare", "-b", "main", cwd=tmp_path)
        subprocess.run(
            [_GIT, "init", "-q", "--bare", "-b", "main", str(self.remote)],
            check=True,
            capture_output=True,
            env=_clean_env(),
        )

        self.repo_main = self.workspace / "myrepo"
        self.repo_main.mkdir()
        _run_git("init", "-q", "-b", "main", cwd=self.repo_main)
        _run_git("config", "user.email", "t@t", cwd=self.repo_main)
        _run_git("config", "user.name", "t", cwd=self.repo_main)
        _run_git("remote", "add", "origin", str(self.remote), cwd=self.repo_main)
        (self.repo_main / "base.txt").write_text("base\n", encoding="utf-8")
        _run_git("add", "-A", cwd=self.repo_main)
        _run_git("commit", "-q", "-m", "initial", cwd=self.repo_main)
        _run_git("push", "-q", "origin", "main", cwd=self.repo_main)
        _run_git("fetch", "-q", "origin", cwd=self.repo_main)

        self.branch = "ac-myrepo-835-x"
        self.wt_path = self.workspace / self.branch / "myrepo"
        _run_git("worktree", "add", "-q", "-b", self.branch, str(self.wt_path), cwd=self.repo_main)

    def _make_worktree(self) -> Worktree:
        ticket = Ticket.objects.create(
            issue_url="https://example.com/issues/835",
            state=Ticket.State.IN_REVIEW,
        )
        return Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="myrepo",
            branch=self.branch,
            extra={"worktree_path": str(self.wt_path)},
        )

    def _prune(self, worktree: Worktree) -> str:
        with (
            patch("teatree.core.cleanup.load_config") as mock_config,
            patch("teatree.core.cleanup.get_overlay") as mock_overlay,
        ):
            mock_config.return_value.user.workspace_dir = self.workspace
            mock_overlay.return_value.get_cleanup_steps.return_value = []
            return cleanup_worktree(worktree, force=True)

    def test_uncommitted_changes_recovered_before_prune(self) -> None:
        # Uncommitted work in the worktree: an edit + a brand-new file.
        (self.wt_path / "base.txt").write_text("base\nDIRTY EDIT\n", encoding="utf-8")
        (self.wt_path / "newfile.txt").write_text("brand new\n", encoding="utf-8")

        self._prune(self._make_worktree())

        assert not self.wt_path.exists(), "worktree must still be removed"
        dirs = _recovery_dirs(self.temp_root)
        assert len(dirs) == 1, f"exactly one recovery dir expected, got {dirs}"
        rec = dirs[0]

        bundle = rec / "branch.bundle"
        diff = rec / "working-tree.diff"
        assert bundle.is_file(), "branch bundle missing"
        assert diff.is_file(), "working-tree diff missing"

        # The bundle is self-contained and verifiable. ``git bundle verify``
        # *requires* an ambient git repository (it checks the bundle's
        # prerequisites against it); run it inside the fixture's own repo via
        # ``-C`` rather than letting git discover one by walking up from the
        # process CWD. The latter is the Docker test-matrix trap: the suite runs
        # with CWD inside a worktree whose ``.git`` gitdir pointer is a host
        # path that does not exist in the container, so an unanchored verify
        # exits 128 ("not a git repository") on a perfectly valid bundle.
        subprocess.run(
            [_GIT, "-C", str(self.repo_main), "bundle", "verify", str(bundle)],
            check=True,
            capture_output=True,
            env=_clean_env(),
        )

        # Reconstruct: clone from the bundle, apply the saved diff — the dirty
        # edit and the untracked file must both come back. ``cwd`` is pinned to
        # the (non-repo) temp root so clone never discovers the broken ambient
        # worktree while resolving config/safe.directory.
        restore = self.temp_root / "restore-dirty"
        subprocess.run(
            [_GIT, "clone", "-q", "-b", self.branch, str(bundle), str(restore)],
            check=True,
            capture_output=True,
            cwd=str(self.temp_root),
            env=_clean_env(),
        )
        subprocess.run(
            [_GIT, "-C", str(restore), "apply", str(diff)],
            check=True,
            capture_output=True,
            env=_clean_env(),
        )
        assert (restore / "base.txt").read_text(encoding="utf-8") == "base\nDIRTY EDIT\n"
        assert (restore / "newfile.txt").read_text(encoding="utf-8") == "brand new\n"

    def test_unpushed_commits_recovered_before_prune(self) -> None:
        # A committed-but-never-pushed change set (clean working tree).
        (self.wt_path / "feature.txt").write_text("feature work\n", encoding="utf-8")
        _run_git("add", "-A", cwd=self.wt_path)
        _run_git("commit", "-q", "-m", "feat: unpushed feature", cwd=self.wt_path)

        self._prune(self._make_worktree())

        assert not self.wt_path.exists()
        dirs = _recovery_dirs(self.temp_root)
        assert len(dirs) == 1, f"exactly one recovery dir expected, got {dirs}"
        bundle = dirs[0] / "branch.bundle"
        assert bundle.is_file(), "branch bundle missing"
        # ``-C`` anchors verify to the fixture repo — see the explanatory
        # comment in test_uncommitted_changes_recovered_before_prune for why an
        # unanchored ``git bundle verify`` exits 128 in the Docker test matrix.
        subprocess.run(
            [_GIT, "-C", str(self.repo_main), "bundle", "verify", str(bundle)],
            check=True,
            capture_output=True,
            env=_clean_env(),
        )
        restore = self.temp_root / "restore-unpushed"
        subprocess.run(
            [_GIT, "clone", "-q", "-b", self.branch, str(bundle), str(restore)],
            check=True,
            capture_output=True,
            cwd=str(self.temp_root),
            env=_clean_env(),
        )
        log = subprocess.run(
            [_GIT, "-C", str(restore), "log", "--format=%s"],
            check=True,
            capture_output=True,
            text=True,
            env=_clean_env(),
        ).stdout
        assert "feat: unpushed feature" in log
        assert (restore / "feature.txt").read_text(encoding="utf-8") == "feature work\n"

    def test_recovered_diff_applies_under_diff_noprefix_config(self) -> None:
        """#835 regression — a user with ``diff.noprefix=true`` must still recover.

        ``git diff`` honours the caller's git config. With ``diff.noprefix=true``
        set (common; was set on the review machine) the produced patch has no
        ``a/``/``b/`` prefixes and a plain ``git apply`` of it FAILS — the
        captured uncommitted work becomes unrestorable (the exact data-loss
        scenario #835 prevents). The capture must force standard prefixes so the
        restore contract holds regardless of user config. The repo-local scope
        is deliberate: do NOT rely on the conftest HOME sandbox, which masks
        real user git config.
        """
        # Repo-local config — survives the HOME sandbox the conftest installs.
        _run_git("config", "diff.noprefix", "true", cwd=self.repo_main)
        _run_git("config", "diff.noprefix", "true", cwd=self.wt_path)

        (self.wt_path / "base.txt").write_text("base\nDIRTY EDIT\n", encoding="utf-8")
        (self.wt_path / "newfile.txt").write_text("brand new\n", encoding="utf-8")

        self._prune(self._make_worktree())

        dirs = _recovery_dirs(self.temp_root)
        assert len(dirs) == 1, f"exactly one recovery dir expected, got {dirs}"
        bundle = dirs[0] / "branch.bundle"
        diff = dirs[0] / "working-tree.diff"
        assert bundle.is_file()
        assert diff.is_file()

        restore = self.temp_root / "restore-noprefix"
        subprocess.run(
            [_GIT, "clone", "-q", "-b", self.branch, str(bundle), str(restore)],
            check=True,
            capture_output=True,
            cwd=str(self.temp_root),
            env=_clean_env(),
        )
        # Plain ``git apply`` — the contract the docstring + BLUEPRINT promise.
        # RED on ``git diff HEAD --binary`` (no prefix flags); GREEN once the
        # capture forces ``--src-prefix=a/ --dst-prefix=b/``.
        subprocess.run(
            [_GIT, "-C", str(restore), "apply", str(diff)],
            check=True,
            capture_output=True,
            env=_clean_env(),
        )
        assert (restore / "base.txt").read_text(encoding="utf-8") == "base\nDIRTY EDIT\n"
        assert (restore / "newfile.txt").read_text(encoding="utf-8") == "brand new\n"

    def test_capture_failure_is_swallowed_and_prune_still_proceeds(self) -> None:
        """#835 — a capture failure must NOT block the prune (stuck-cleanup).

        Drives the production seam (``cleanup_worktree`` → ``_remove_git_worktree``)
        with the real on-disk worktree; only the (unstoppable, deliberately
        failing) capture is patched to raise. The ticket mandates: swallow the
        exception, still remove the worktree, surface the failure in the label.
        """
        (self.wt_path / "base.txt").write_text("base\nDIRTY EDIT\n", encoding="utf-8")
        wt = self._make_worktree()
        boom = RuntimeError("disk full while bundling")

        with (
            patch("teatree.core.cleanup.load_config") as mock_config,
            patch("teatree.core.cleanup.get_overlay") as mock_overlay,
            patch("teatree.core.cleanup.capture_recovery_artifact", side_effect=boom),
        ):
            mock_config.return_value.user.workspace_dir = self.workspace
            mock_overlay.return_value.get_cleanup_steps.return_value = []
            label = cleanup_worktree(wt, force=True)  # must NOT raise

        assert not self.wt_path.exists(), "worktree must still be removed despite capture failure"
        assert f"recovery capture failed for {self.branch}" in label
        assert "disk full while bundling" in label
        assert _recovery_dirs(self.temp_root) == [], "no artifact when capture itself failed"

    def test_clean_merged_worktree_hard_deletes_with_no_artifact(self) -> None:
        # Branch tip == origin/main, clean working tree: nothing to lose.
        _run_git("push", "-q", "origin", f"{self.branch}:main", cwd=self.repo_main)
        _run_git("fetch", "-q", "origin", cwd=self.repo_main)

        self._prune(self._make_worktree())

        assert not self.wt_path.exists(), "clean worktree must hard-delete"
        assert _recovery_dirs(self.temp_root) == [], "no recovery artifact for clean+merged worktree"


class TestWorktreeRecoveryEdgeCases(TestCase):
    """#835 — defensive branches of the recovery capture helper."""

    @pytest.fixture(autouse=True)
    def _inject(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    def _worktree(self) -> Worktree:
        ticket = Ticket.objects.create(
            issue_url="https://example.com/issues/835",
            state=Ticket.State.IN_REVIEW,
        )
        return Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="myrepo",
            branch="ac-myrepo-835-x",
        )

    def test_returns_none_when_worktree_dir_absent(self) -> None:
        missing = "/nonexistent/worktree/path/that/does/not/exist"
        result = capture_recovery_artifact(Path("/nonexistent/repo"), missing, self._worktree())
        assert result is None

    def test_unpushed_probe_failure_fails_open_to_capture(self) -> None:
        """An inconclusive probe must be treated as "might have unpushed work"."""
        with patch(
            "teatree.core.worktree_recovery.git.commits_absent_from_all_remotes",
            side_effect=CommandFailedError(["git"], 128, "", "corrupt"),
        ):
            assert _has_unpushed_commits(Path("/repo"), "some-branch") is True

    def test_bundle_failure_removes_partial_dir_and_reraises(self) -> None:
        """A failed capture must not leave a stray temp dir and must re-raise."""
        temp_root = self.tmp_path / "systmp"
        temp_root.mkdir()
        wt = self.tmp_path / "wt"
        wt.mkdir()
        self.monkeypatch.setattr("teatree.core.worktree_recovery.tempfile.gettempdir", lambda: str(temp_root))
        with (
            patch("teatree.core.worktree_recovery.git.status_porcelain", return_value=" M f"),
            patch("teatree.core.worktree_recovery.git.commits_absent_from_all_remotes", return_value=[]),
            patch(
                "teatree.core.worktree_recovery.git.bundle_create",
                side_effect=CommandFailedError(["git"], 128, "", "no repo"),
            ),
            pytest.raises(CommandFailedError),
        ):
            capture_recovery_artifact(self.tmp_path / "repo", str(wt), self._worktree())
        assert list(temp_root.iterdir()) == [], "partial recovery dir must be cleaned up"
