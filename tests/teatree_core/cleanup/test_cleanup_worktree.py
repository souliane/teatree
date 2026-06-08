"""``cleanup_worktree`` orchestration behaviour with the git layer mocked.

Split verbatim from the former monolithic ``tests/teatree_core/test_cleanup.py``
(souliane/teatree#443). The classifier-driven safety gates, redis-slot release
and overlay-step wiring all exercise the wholesale ``teatree.core.cleanup.git``
mock; the shared module-level ``_patch_*`` decorators and the
``_no_unpushed``/``_mock_workspace`` helpers are lifted unchanged.
"""

from unittest.mock import ANY, MagicMock, patch

import pytest
from django.test import TestCase

from teatree.core.cleanup import BranchClassification, BranchCommit, cleanup_worktree
from teatree.core.models import Ticket, Worktree
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
        result = cleanup_worktree(wt)

        mock_git.worktree_remove.assert_called_once()
        mock_git.branch_delete.assert_called_once()
        assert result.clean is True
        assert "org/repo" in result.label
        assert "fix-99" in result.label
        assert not Worktree.objects.filter(pk=wt_id).exists()

    @_patch_overlay
    @_patch_git
    @_patch_config
    def test_stops_docker_compose_project_to_avoid_leaking_containers(
        self,
        mock_config: MagicMock,
        mock_git: MagicMock,
        mock_overlay: MagicMock,
    ) -> None:
        """Regression for #1306: `cleanup_worktree` must stop the docker compose project.

        Pre-fix only `WorktreeTeardownRunner` called `docker_compose_down`,
        but `cleanup_worktree` itself was also reached by the FSM-merged
        auto-teardown path (`WorktreeTeardown`), `clean-merged`, and
        `clean-all`. Those paths left containers running on host ports
        5432/6379, blocking the next `worktree provision` with a port
        conflict. Wiring docker-down into `cleanup_worktree` makes the
        promise in the docstring ("Stop docker, drop DB, remove git
        worktree, delete row") hold for every caller.
        """
        _mock_workspace(mock_config)
        _no_unpushed(mock_git)
        mock_overlay.return_value.get_cleanup_steps.return_value = []
        mock_git.status_porcelain.return_value = ""

        wt = self._make_worktree(wt_path="/tmp/wt/org/repo")
        with patch("teatree.core.runners.worktree_start.docker_compose_down") as mock_down:
            cleanup_worktree(wt)
        # The compose project name follows `{repo_path}-wt{ticket_number}`.
        ((project,), _kwargs) = mock_down.call_args
        assert project.startswith("org/repo-wt")

    @_patch_overlay
    @_patch_git
    @_patch_config
    def test_invokes_overlay_external_resource_reaper(
        self,
        mock_config: MagicMock,
        mock_git: MagicMock,
        mock_overlay: MagicMock,
    ) -> None:
        """#1523: cleanup hands each reaped worktree to the overlay's reaper.

        ``docker_compose_down`` removes containers but never images, so the
        per-worktree application image (~9GB) lingered for every removed
        worktree. ``cleanup_worktree`` now calls
        ``reap_worktree_external_resources`` for the docker-using overlay to
        remove that worktree's compose images + containers in the same pass.
        """
        _mock_workspace(mock_config)
        _no_unpushed(mock_git)
        mock_overlay.return_value.get_cleanup_steps.return_value = []
        mock_overlay.return_value.reap_worktree_external_resources.return_value = ["reaped 1 image"]
        mock_git.status_porcelain.return_value = ""

        wt = self._make_worktree(wt_path="/tmp/wt/org/repo")
        result = cleanup_worktree(wt)

        mock_overlay.return_value.reap_worktree_external_resources.assert_called_once_with(wt)
        assert "reaped 1 image" in result.label

    @_patch_overlay
    @_patch_git
    @_patch_config
    def test_overlay_reaper_failure_is_surfaced_not_crashed(
        self,
        mock_config: MagicMock,
        mock_git: MagicMock,
        mock_overlay: MagicMock,
    ) -> None:
        _mock_workspace(mock_config)
        _no_unpushed(mock_git)
        mock_overlay.return_value.get_cleanup_steps.return_value = []
        mock_overlay.return_value.reap_worktree_external_resources.side_effect = RuntimeError("docker exploded")
        mock_git.status_porcelain.return_value = ""

        wt = self._make_worktree(wt_path="/tmp/wt/org/repo")
        wt_id = wt.pk
        result = cleanup_worktree(wt)

        assert not result.clean
        assert any("docker exploded" in e for e in result.errors)
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
            mock_drop.assert_called_once_with("wt_99", user="", host="", env=None)

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
            patch("teatree.core.branch_classification._pr_merge_commit_sha", return_value=""),
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

        wt = self._make_worktree(wt_path="/tmp/wt/org/repo")
        # The squash-tree match (PR merge commit tree == branch tip) is the
        # safe-to-remove signal — control it at cleanup's call site.
        with patch("teatree.core.cleanup._branch_tree_matches_squash", return_value=True):
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
            patch("teatree.core.branch_classification._pr_merge_commit_sha", return_value="squash123"),
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

    @_patch_classify
    @_patch_overlay
    @_patch_git
    @_patch_config
    def test_reaps_genuinely_ahead_branch_when_forge_says_pr_merged(
        self,
        mock_config: MagicMock,
        mock_git: MagicMock,
        mock_overlay: MagicMock,
        mock_classify: MagicMock,
    ) -> None:
        """#1578 — a long-diverged branch whose PR the forge reports MERGED is reaped.

        The subject-match classifier reports ``genuinely_ahead`` and the squash
        tree no longer matches the branch tip (the branch diverged long ago), so
        the prior guards refuse. The canonical forge PR-state check overrides:
        a merged PR is the ground truth that the work shipped.
        """
        _mock_workspace(mock_config)
        _no_unpushed(mock_git)
        mock_overlay.return_value.get_cleanup_steps.return_value = []
        mock_git.status_porcelain.return_value = ""
        mock_git.unsynced_commits.return_value = ["abc123 feat: shipped via squash long ago"]
        mock_classify.return_value = BranchClassification(
            genuinely_ahead=[BranchCommit(sha="abc123", subject="feat: shipped via squash long ago", is_merge=False)]
        )
        mock_git.check.return_value = False  # tree differs — branch tip diverged from squash

        wt = self._make_worktree(wt_path="/tmp/wt/org/repo")
        with (
            patch("teatree.core.branch_classification._pr_merge_commit_sha", return_value="squash123"),
            patch("teatree.core.cleanup._branch_pr_is_merged", return_value=True),
        ):
            cleanup_worktree(wt)

        mock_git.worktree_remove.assert_called_once()
        mock_git.branch_delete.assert_called_once()

    @_patch_classify
    @_patch_overlay
    @_patch_git
    @_patch_config
    def test_refuses_genuinely_ahead_branch_when_no_merged_pr(
        self,
        mock_config: MagicMock,
        mock_git: MagicMock,
        mock_overlay: MagicMock,
        mock_classify: MagicMock,
    ) -> None:
        """#1578 load-bearing safety test — real pending work (no merged PR) is still refused.

        The forge canonically reports no merged PR for the branch, so the
        conservative refuse-and-report stands: genuinely-ahead work is never
        auto-discarded.
        """
        _mock_workspace(mock_config)
        _no_unpushed(mock_git)
        mock_overlay.return_value.get_cleanup_steps.return_value = []
        mock_git.status_porcelain.return_value = ""
        mock_git.unsynced_commits.return_value = ["abc123 feat: genuine unpushed work"]
        mock_classify.return_value = BranchClassification(
            genuinely_ahead=[BranchCommit(sha="abc123", subject="feat: genuine unpushed work", is_merge=False)]
        )
        mock_git.check.return_value = False  # tree differs — not captured by any squash

        wt = self._make_worktree(wt_path="/tmp/wt/org/repo")
        with (
            patch("teatree.core.branch_classification._pr_merge_commit_sha", return_value=""),
            patch("teatree.core.cleanup._branch_pr_is_merged", return_value=False),
            pytest.raises(RuntimeError, match="unsynced commit"),
        ):
            cleanup_worktree(wt)

        mock_git.worktree_remove.assert_not_called()
        mock_git.branch_delete.assert_not_called()

    @_patch_overlay
    @_patch_git
    @_patch_config
    def test_reaps_unpushed_branch_when_forge_says_pr_merged(
        self,
        mock_config: MagicMock,
        mock_git: MagicMock,
        mock_overlay: MagicMock,
    ) -> None:
        """#1578 — a branch flagged 'commits on NO remote' is reaped when its PR is MERGED.

        Squash-merge creates a new SHA on main, so the branch's own SHAs are
        absent from every remote ref — the #706 data-loss guard fires. But the
        forge canonically reports the branch's PR merged, so the content shipped
        and the worktree is safe to reap.
        """
        _mock_workspace(mock_config)
        _no_unpushed(mock_git)
        mock_overlay.return_value.get_cleanup_steps.return_value = []
        mock_git.status_porcelain.return_value = ""
        mock_git.unsynced_commits.return_value = []
        mock_git.commits_absent_from_all_remotes.return_value = ["abc1234 feat: squashed onto main"]

        wt = self._make_worktree(wt_path="/tmp/wt/org/repo")
        with patch("teatree.core.cleanup._branch_pr_is_merged", return_value=True):
            cleanup_worktree(wt)

        mock_git.worktree_remove.assert_called_once()
        mock_git.branch_delete.assert_called_once()

    @_patch_overlay
    @_patch_git
    @_patch_config
    def test_refuses_unpushed_branch_when_forge_lookup_uncertain(
        self,
        mock_config: MagicMock,
        mock_git: MagicMock,
        mock_overlay: MagicMock,
    ) -> None:
        """#1578 fail-safe — an unpushed branch with no/uncertain merged PR is still refused.

        The forge lookup returning ``False`` (no merged PR found, CLI missing,
        or any probe failure) leaves the #706 data-loss guard in force: ambiguity
        never reaps.
        """
        _mock_workspace(mock_config)
        _no_unpushed(mock_git)
        mock_overlay.return_value.get_cleanup_steps.return_value = []
        mock_git.status_porcelain.return_value = ""
        mock_git.commits_absent_from_all_remotes.return_value = ["abc1234 feat: never pushed, no PR"]

        wt = self._make_worktree(wt_path="/tmp/wt/org/repo")
        with (
            patch("teatree.core.cleanup._branch_pr_is_merged", return_value=False),
            pytest.raises(RuntimeError, match=r"on NO remote \(data loss\)"),
        ):
            cleanup_worktree(wt)

        mock_git.worktree_remove.assert_not_called()
        mock_git.branch_delete.assert_not_called()

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
            patch("teatree.core.branch_classification._pr_merge_commit_sha", return_value=""),
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


class TestCleanupWorktreeLoudTeardown(TestCase):
    """#877 — teardown failures surface in ``CleanupResult.errors``.

    The loud-teardown half of #877: no ``suppress(Exception)`` / silent
    swallowing on the teardown path. A failing resource (DB drop, pass
    entry removal, overlay step) is recorded as a descriptive error string
    and the *other* resources are still reaped — collect-and-surface, never
    crash mid-teardown leaving orphans.
    """

    def _make_worktree(self, *, db_name: str = "wt_99") -> Worktree:
        ticket = Ticket.objects.create(
            issue_url="https://gitlab.com/org/repo/-/issues/99",
            state=Ticket.State.IN_REVIEW,
        )
        return Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="org/repo",
            branch="fix-99",
            db_name=db_name,
            extra={"worktree_path": "/tmp/wt/org/repo"},
        )

    @_patch_overlay
    @_patch_git
    @_patch_config
    def test_db_drop_failure_surfaced_other_resources_still_reaped(
        self,
        mock_config: MagicMock,
        mock_git: MagicMock,
        mock_overlay: MagicMock,
    ) -> None:
        """A failed ``dropdb`` is recorded in ``errors``; the row + pass entry still go.

        Before #877 this either crashed mid-teardown (leaving the Worktree
        row, redis slot and pass entry orphaned) or was swallowed. Now the
        failure is a descriptive ``errors`` entry, the result is non-clean,
        and every other resource is still cleaned.
        """
        _mock_workspace(mock_config)
        _no_unpushed(mock_git)
        mock_overlay.return_value.get_cleanup_steps.return_value = []
        mock_overlay.return_value.config.teardown_removes_pass_entries = True
        mock_git.status_porcelain.return_value = ""
        mock_git.unsynced_commits.return_value = []

        wt = self._make_worktree(db_name="wt_99")
        wt_id = wt.pk
        ticket_number = wt.ticket.ticket_number

        with (
            patch(
                "teatree.core.cleanup.drop_db",
                side_effect=CommandFailedError(["dropdb", "wt_99"], 1, "", "connection refused"),
            ),
            patch("teatree.core.cleanup.remove_postgres_pass_entry") as mock_remove,
        ):
            result = cleanup_worktree(wt)

        # Failure surfaced, not swallowed
        assert result.clean is False
        assert any("wt_99" in e for e in result.errors)
        assert any("connection refused" in e for e in result.errors)
        # Other resources STILL reaped despite the DB-drop failure
        mock_remove.assert_called_once_with(ticket_number)
        mock_git.worktree_remove.assert_called_once()
        assert not Worktree.objects.filter(pk=wt_id).exists()

    @_patch_overlay
    @_patch_git
    @_patch_config
    def test_pass_entry_removal_failure_surfaced(
        self,
        mock_config: MagicMock,
        mock_git: MagicMock,
        mock_overlay: MagicMock,
    ) -> None:
        """A failing pass-entry removal is surfaced, the worktree row still deleted."""
        _mock_workspace(mock_config)
        _no_unpushed(mock_git)
        mock_overlay.return_value.get_cleanup_steps.return_value = []
        mock_overlay.return_value.config.teardown_removes_pass_entries = True
        mock_git.status_porcelain.return_value = ""
        mock_git.unsynced_commits.return_value = []

        wt = self._make_worktree(db_name="")
        wt_id = wt.pk

        with (
            patch("teatree.core.cleanup.drop_db"),
            patch(
                "teatree.core.cleanup.remove_postgres_pass_entry",
                side_effect=RuntimeError("pass: gpg failed"),
            ),
        ):
            result = cleanup_worktree(wt)

        assert result.clean is False
        assert any("gpg failed" in e for e in result.errors)
        assert not Worktree.objects.filter(pk=wt_id).exists()

    @_patch_overlay
    @_patch_git
    @_patch_config
    def test_overlay_step_failure_surfaced_in_errors_not_only_label(
        self,
        mock_config: MagicMock,
        mock_git: MagicMock,
        mock_overlay: MagicMock,
    ) -> None:
        """Overlay-step failures reach ``errors`` (the operator channel), not just the label string.

        #932's lesson: a swallowed string the caller never inspects is not
        surfacing. The structured ``errors`` list is what sync backends push
        into ``SyncResult.errors``.
        """
        _mock_workspace(mock_config)
        _no_unpushed(mock_git)
        failing_step = MagicMock(callable=MagicMock(side_effect=RuntimeError("docker compose down failed")))
        failing_step.description = "stop docker stack"
        mock_overlay.return_value.get_cleanup_steps.return_value = [failing_step]
        mock_git.status_porcelain.return_value = ""
        mock_git.unsynced_commits.return_value = []

        wt = self._make_worktree(db_name="")
        result = cleanup_worktree(wt)

        assert result.clean is False
        assert any("docker compose down failed" in e for e in result.errors)

    @_patch_overlay
    @_patch_git
    @_patch_config
    def test_clean_teardown_has_empty_errors_and_is_clean(
        self,
        mock_config: MagicMock,
        mock_git: MagicMock,
        mock_overlay: MagicMock,
    ) -> None:
        """The happy path: no errors, ``clean`` is true, label still contains the worktree."""
        _mock_workspace(mock_config)
        _no_unpushed(mock_git)
        mock_overlay.return_value.get_cleanup_steps.return_value = []
        mock_git.status_porcelain.return_value = ""
        mock_git.unsynced_commits.return_value = []

        wt = self._make_worktree(db_name="")
        with patch("teatree.core.cleanup.drop_db"):
            result = cleanup_worktree(wt)

        assert result.clean is True
        assert result.errors == []
        assert "org/repo" in result.label
        assert "org/repo" in str(result)
