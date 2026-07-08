"""``cleanup_worktree`` orchestration behaviour with the git layer mocked.

Split verbatim from the former monolithic ``tests/teatree_core/test_cleanup.py``
(souliane/teatree#443). The classifier-driven safety gates and overlay-step
wiring all exercise the wholesale ``teatree.core.cleanup.cleanup.git``
mock; the shared module-level ``_patch_*`` decorators and the
``_no_unpushed``/``_mock_workspace`` helpers are lifted unchanged.
"""

from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.cleanup.cleanup import WorktreeBusyError, cleanup_worktree
from teatree.core.models import Session, Task, Ticket, Worktree
from teatree.core.models.external_delivery import mark_external_delivery
from teatree.core.overlay import OverlayBase, OverlayRuntime, ProvisionStep, RunCommands
from teatree.utils.run import CommandFailedError
from tests.teatree_core._provision_timebox_stub import provision_timebox_unimportable

_patch_config = patch("teatree.core.cleanup.cleanup.clone_root")
_patch_git = patch("teatree.core.cleanup.cleanup.git")
_patch_overlay = patch("teatree.core.cleanup.cleanup.get_overlay_for_worktree")
# The origin/main hygiene gate is now authorized by the CONTENT gate (#2609),
# not subject-match — so a test that drives the gate patches
# ``content_equivalence_blockers``, the helper every destructive caller funnels
# through, rather than the cheap ``classify_branch_commits`` recognizer.
_patch_content = patch("teatree.core.cleanup.cleanup.content_equivalence_blockers")
# Pin the #2205 merged-evidence override to False so tests that set
# ``commits_absent_from_all_remotes`` to a non-empty list still hit the
# data-loss guard rather than silently passing through the squash-merge override.
_patch_ref_tree = patch("teatree.core.cleanup.cleanup._ref_captured_by_merge", return_value=False)


def _no_unpushed(mock_git: MagicMock) -> None:
    """Default the #706 + #2609 hygiene gates to "nothing to lose" on the wholesale git mock.

    Tests exercising unrelated cleanup behaviour share the wholesale ``cleanup.git``
    mock; without this the #706 guard sees a truthy ``MagicMock`` and refuses
    spuriously. The #2609 content gate (``content_equivalence_blockers``) lives in
    ``branch_classification`` and runs real ``git`` — so it is short-circuited here
    by defaulting ``unsynced_commits`` to empty (a fully-synced branch never reaches
    the content gate). Tests that target a guard override the relevant value after
    calling this.
    """
    mock_git.commits_absent_from_all_remotes.return_value = []
    mock_git.unsynced_commits.return_value = []


def _mock_workspace(mock_config: MagicMock) -> None:
    mock_config.return_value.__truediv__ = lambda self, x: MagicMock(is_dir=lambda: True)


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
        mock_overlay.return_value.provisioning.cleanup_steps.return_value = []
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
        mock_overlay.return_value.provisioning.cleanup_steps.return_value = []
        mock_git.status_porcelain.return_value = ""

        wt = self._make_worktree(wt_path="/tmp/wt/org/repo")
        with patch("teatree.core.runners.worktree_start.docker_compose_down") as mock_down:
            cleanup_worktree(wt)
        # The compose project name follows `{repo_path}-wt{ticket.pk}`.
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
        ``provisioning.reap_external_resources`` for the docker-using overlay to
        remove that worktree's compose images + containers in the same pass.
        """
        _mock_workspace(mock_config)
        _no_unpushed(mock_git)
        mock_overlay.return_value.provisioning.cleanup_steps.return_value = []
        mock_overlay.return_value.provisioning.reap_external_resources.return_value = ["reaped 1 image"]
        mock_git.status_porcelain.return_value = ""

        wt = self._make_worktree(wt_path="/tmp/wt/org/repo")
        result = cleanup_worktree(wt)

        mock_overlay.return_value.provisioning.reap_external_resources.assert_called_once_with(wt)
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
        mock_overlay.return_value.provisioning.cleanup_steps.return_value = []
        mock_overlay.return_value.provisioning.reap_external_resources.side_effect = RuntimeError("docker exploded")
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
        mock_overlay.return_value.provisioning.cleanup_steps.return_value = []

        wt = self._make_worktree(db_name="wt_99")

        with patch("teatree.core.cleanup.cleanup.drop_db") as mock_drop:
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
        mock_overlay.return_value.provisioning.cleanup_steps.return_value = [MagicMock(callable=step_fn)]

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
        mock_overlay.return_value.provisioning.cleanup_steps.return_value = []
        mock_overlay.return_value.config.teardown_removes_pass_entries = False

        wt = self._make_worktree(wt_path="/tmp/wt/org/repo")
        with patch("teatree.core.cleanup.cleanup.remove_postgres_pass_entry") as mock_remove:
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
        mock_overlay.return_value.provisioning.cleanup_steps.return_value = []
        mock_overlay.return_value.config.teardown_removes_pass_entries = True

        wt = self._make_worktree(wt_path="/tmp/wt/org/repo")
        with patch("teatree.core.cleanup.cleanup.remove_postgres_pass_entry") as mock_remove:
            cleanup_worktree(wt)
        # Pass key is ticket-pk-scoped (canonical, unique), not ticket_number.
        mock_remove.assert_called_once_with(wt.ticket_id)

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
        mock_overlay.return_value.provisioning.cleanup_steps.return_value = []

        wt = self._make_worktree()
        wt_id = wt.pk
        cleanup_worktree(wt)

        assert not Worktree.objects.filter(pk=wt_id).exists()

    @_patch_content
    @_patch_overlay
    @_patch_git
    @_patch_config
    def test_raises_when_content_not_upstream(
        self,
        mock_config: MagicMock,
        mock_git: MagicMock,
        mock_overlay: MagicMock,
        mock_content: MagicMock,
    ) -> None:
        """A commit the content gate cannot prove upstream blocks cleanup (#2609)."""
        _mock_workspace(mock_config)
        _no_unpushed(mock_git)
        mock_overlay.return_value.provisioning.cleanup_steps.return_value = []
        mock_git.status_porcelain.return_value = ""
        mock_git.unsynced_commits.return_value = ["abc123 chore: cve fix"]
        mock_content.return_value = ["abc123"]  # patch NOT upstream → blocker

        wt = self._make_worktree(wt_path="/tmp/wt/org/repo")
        with (
            patch("teatree.core.cleanup.cleanup._branch_tree_matches_squash", return_value=False),
            patch("teatree.core.cleanup.cleanup._branch_pr_is_merged", return_value=False),
            pytest.raises(RuntimeError, match="content not upstream"),
        ):
            cleanup_worktree(wt)

        mock_git.worktree_remove.assert_not_called()
        mock_git.branch_delete.assert_not_called()

    @_patch_content
    @_patch_overlay
    @_patch_git
    @_patch_config
    def test_cleans_when_content_not_upstream_but_tree_matches_pr_squash_commit(
        self,
        mock_config: MagicMock,
        mock_git: MagicMock,
        mock_overlay: MagicMock,
        mock_content: MagicMock,
    ) -> None:
        """Post-merge follow-ups tree-equal to PR squash are safe to clean.

        The content gate reports a blocker (the patch-id differs from the squash),
        but the cumulative tree matches the PR's squash commit, so the content is
        already in main. Reproduces the common case where an agent pushes
        retro/docs commits AFTER the PR was squash-merged.
        """
        _mock_workspace(mock_config)
        _no_unpushed(mock_git)
        mock_overlay.return_value.provisioning.cleanup_steps.return_value = []
        mock_git.status_porcelain.return_value = ""
        mock_git.unsynced_commits.return_value = ["abc123 retro: post-merge docs"]
        mock_content.return_value = ["abc123"]  # patch differs from squash → blocker

        wt = self._make_worktree(wt_path="/tmp/wt/org/repo")
        # The squash-tree match (PR merge commit tree == branch tip) is the
        # positive merged-evidence override — control it at cleanup's call site.
        with patch("teatree.core.cleanup.cleanup._branch_tree_matches_squash", return_value=True):
            cleanup_worktree(wt)

        mock_git.worktree_remove.assert_called_once()
        mock_git.branch_delete.assert_called_once()

    @_patch_content
    @_patch_overlay
    @_patch_git
    @_patch_config
    def test_raises_when_content_not_upstream_and_tree_differs_from_pr_squash_commit(
        self,
        mock_config: MagicMock,
        mock_git: MagicMock,
        mock_overlay: MagicMock,
        mock_content: MagicMock,
    ) -> None:
        """A content blocker whose tree differs from the squash carries real work."""
        _mock_workspace(mock_config)
        _no_unpushed(mock_git)
        mock_overlay.return_value.provisioning.cleanup_steps.return_value = []
        mock_git.status_porcelain.return_value = ""
        mock_git.unsynced_commits.return_value = ["abc123 feat: new work"]
        mock_content.return_value = ["abc123"]

        wt = self._make_worktree(wt_path="/tmp/wt/org/repo")
        with (
            patch("teatree.core.cleanup.cleanup._branch_tree_matches_squash", return_value=False),
            patch("teatree.core.cleanup.cleanup._branch_pr_is_merged", return_value=False),
            pytest.raises(RuntimeError, match="content not upstream"),
        ):
            cleanup_worktree(wt)

    @_patch_content
    @_patch_overlay
    @_patch_git
    @_patch_config
    def test_cleans_when_content_is_proven_upstream(
        self,
        mock_config: MagicMock,
        mock_git: MagicMock,
        mock_overlay: MagicMock,
        mock_content: MagicMock,
    ) -> None:
        """Branches whose commits are content-equivalent upstream are safe to clean.

        The content gate returns no blocker (every unique commit is patch-equivalent
        upstream, or only merge/squash-merged commits remain), so the worktree is
        removed without any forge query.
        """
        _mock_workspace(mock_config)
        _no_unpushed(mock_git)
        mock_overlay.return_value.provisioning.cleanup_steps.return_value = []
        mock_git.status_porcelain.return_value = ""
        mock_git.unsynced_commits.return_value = ["abc123 feat: squashed on main"]
        mock_content.return_value = []  # content proven upstream

        wt = self._make_worktree(wt_path="/tmp/wt/org/repo")
        cleanup_worktree(wt)

        mock_git.worktree_remove.assert_called_once()
        mock_git.branch_delete.assert_called_once()

    @_patch_content
    @_patch_overlay
    @_patch_git
    @_patch_config
    def test_reaps_content_blocked_branch_when_forge_says_pr_merged(
        self,
        mock_config: MagicMock,
        mock_git: MagicMock,
        mock_overlay: MagicMock,
        mock_content: MagicMock,
    ) -> None:
        """#1578 — a long-diverged branch whose PR the forge reports MERGED is reaped.

        The content gate reports a blocker (the squash created a new SHA so the
        patch-id no longer matches) and the squash tree no longer matches the
        branch tip, so the prior signals refuse. The canonical forge PR-state
        check overrides: a merged PR is the ground truth that the work shipped.
        """
        _mock_workspace(mock_config)
        _no_unpushed(mock_git)
        mock_overlay.return_value.provisioning.cleanup_steps.return_value = []
        mock_git.status_porcelain.return_value = ""
        mock_git.unsynced_commits.return_value = ["abc123 feat: shipped via squash long ago"]
        mock_content.return_value = ["abc123"]

        wt = self._make_worktree(wt_path="/tmp/wt/org/repo")
        with (
            patch("teatree.core.cleanup.cleanup._branch_tree_matches_squash", return_value=False),
            patch("teatree.core.cleanup.cleanup._branch_pr_is_merged", return_value=True),
        ):
            cleanup_worktree(wt)

        mock_git.worktree_remove.assert_called_once()
        mock_git.branch_delete.assert_called_once()

    @_patch_content
    @_patch_overlay
    @_patch_git
    @_patch_config
    def test_refuses_content_blocked_branch_when_no_merged_pr(
        self,
        mock_config: MagicMock,
        mock_git: MagicMock,
        mock_overlay: MagicMock,
        mock_content: MagicMock,
    ) -> None:
        """#1578/#2609 load-bearing safety test — real pending work (no merged PR) is still refused.

        The content gate reports a blocker and the forge canonically reports no
        merged PR, so the conservative refuse-and-report stands: genuine work is
        never auto-discarded on a subject match alone.
        """
        _mock_workspace(mock_config)
        _no_unpushed(mock_git)
        mock_overlay.return_value.provisioning.cleanup_steps.return_value = []
        mock_git.status_porcelain.return_value = ""
        mock_git.unsynced_commits.return_value = ["abc123 feat: genuine unpushed work"]
        mock_content.return_value = ["abc123"]

        wt = self._make_worktree(wt_path="/tmp/wt/org/repo")
        with (
            patch("teatree.core.cleanup.cleanup._branch_tree_matches_squash", return_value=False),
            patch("teatree.core.cleanup.cleanup._branch_pr_is_merged", return_value=False),
            pytest.raises(RuntimeError, match="content not upstream"),
        ):
            cleanup_worktree(wt)

        mock_git.worktree_remove.assert_not_called()
        mock_git.branch_delete.assert_not_called()

    @_patch_overlay
    @_patch_git
    @_patch_config
    def test_reaps_unpushed_branch_when_merged_evidence_confirms_capture(
        self,
        mock_config: MagicMock,
        mock_git: MagicMock,
        mock_overlay: MagicMock,
    ) -> None:
        """#1578/#2205 — a branch flagged 'commits on NO remote' is reaped when MERGED-evidence confirms it.

        Squash-merge creates a new SHA on main, so the branch's own SHAs are
        absent from every remote ref — the #706 data-loss guard fires. But
        positive merged-evidence (forge PR merged / pre-prune tracking ref) AND a
        matching tree confirm the content shipped, so the worktree is safe to reap.
        """
        _mock_workspace(mock_config)
        _no_unpushed(mock_git)
        mock_overlay.return_value.provisioning.cleanup_steps.return_value = []
        mock_git.status_porcelain.return_value = ""
        mock_git.unsynced_commits.return_value = []
        mock_git.commits_absent_from_all_remotes.return_value = ["abc1234 feat: squashed onto main"]

        wt = self._make_worktree(wt_path="/tmp/wt/org/repo")
        with patch("teatree.core.cleanup.cleanup._ref_captured_by_merge", return_value=True):
            cleanup_worktree(wt)

        mock_git.worktree_remove.assert_called_once()
        mock_git.branch_delete.assert_called_once()

    @_patch_ref_tree
    @_patch_overlay
    @_patch_git
    @_patch_config
    def test_refuses_unpushed_branch_when_merged_evidence_absent(
        self,
        mock_config: MagicMock,
        mock_git: MagicMock,
        mock_overlay: MagicMock,
        mock_ref_tree: MagicMock,
    ) -> None:
        """#1578/#2205 fail-safe — an unpushed branch with no merged-evidence is still refused.

        With no positive merged-evidence (no merged PR, no pre-prune tracking ref,
        tree match alone insufficient) the #706 data-loss guard stays in force:
        ambiguity never reaps.
        """
        _mock_workspace(mock_config)
        _no_unpushed(mock_git)
        mock_overlay.return_value.provisioning.cleanup_steps.return_value = []
        mock_git.status_porcelain.return_value = ""
        mock_git.commits_absent_from_all_remotes.return_value = ["abc1234 feat: never pushed, no PR"]

        wt = self._make_worktree(wt_path="/tmp/wt/org/repo")
        with pytest.raises(RuntimeError, match=r"on NO remote \(data loss\)"):
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
        mock_overlay.return_value.provisioning.cleanup_steps.return_value = []
        mock_git.status_porcelain.return_value = ""
        mock_git.unsynced_commits.return_value = ["abc123 chore: cve fix"]

        wt = self._make_worktree(wt_path="/tmp/wt/org/repo")
        cleanup_worktree(wt, force=True)

        mock_git.worktree_remove.assert_called_once()
        mock_git.branch_delete.assert_called_once()

    @_patch_ref_tree
    @_patch_overlay
    @_patch_git
    @_patch_config
    def test_raises_when_commits_absent_from_all_remotes(
        self,
        mock_config: MagicMock,
        mock_git: MagicMock,
        mock_overlay: MagicMock,
        mock_ref_tree: MagicMock,
    ) -> None:
        """#706 data-loss guard — branch with commits on no remote blocks teardown."""
        _mock_workspace(mock_config)
        _no_unpushed(mock_git)
        mock_overlay.return_value.provisioning.cleanup_steps.return_value = []
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

    @_patch_ref_tree
    @_patch_overlay
    @_patch_git
    @_patch_config
    def test_unpushed_guard_message_truncates_sha_preview(
        self,
        mock_config: MagicMock,
        mock_git: MagicMock,
        mock_overlay: MagicMock,
        mock_ref_tree: MagicMock,
    ) -> None:
        """More than the preview limit of unpushed commits is summarised with an ellipsis."""
        _mock_workspace(mock_config)
        _no_unpushed(mock_git)
        mock_overlay.return_value.provisioning.cleanup_steps.return_value = []
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
        mock_overlay.return_value.provisioning.cleanup_steps.return_value = []
        mock_git.status_porcelain.return_value = ""
        mock_git.commits_absent_from_all_remotes.return_value = ["abc123 feat: unpushed"]

        wt = self._make_worktree(wt_path="/tmp/wt/org/repo")
        cleanup_worktree(wt, force=True)

        mock_git.worktree_remove.assert_called_once()
        mock_git.commits_absent_from_all_remotes.assert_not_called()

    @_patch_content
    @_patch_overlay
    @_patch_git
    @_patch_config
    def test_strict_hygiene_refuses_pushed_but_unmerged_branch(
        self,
        mock_config: MagicMock,
        mock_git: MagicMock,
        mock_overlay: MagicMock,
        mock_content: MagicMock,
    ) -> None:
        """Pushed-but-unmerged branch is refused under strict hygiene (default).

        The origin/main hygiene gate still blocks it — the sync-backend /
        clean-all contract is unchanged. A branch pushed to its own ref survives
        the #706 data-loss guard, but its content is not on origin/main, so the
        content gate refuses it.
        """
        _mock_workspace(mock_config)
        _no_unpushed(mock_git)
        mock_overlay.return_value.provisioning.cleanup_steps.return_value = []
        mock_git.status_porcelain.return_value = ""
        mock_git.commits_absent_from_all_remotes.return_value = []  # pushed → data-loss guard passes
        mock_git.unsynced_commits.return_value = ["abc123 feat: pushed not merged"]
        mock_content.return_value = ["abc123"]  # content not on origin/main

        wt = self._make_worktree(wt_path="/tmp/wt/org/repo")
        with (
            patch("teatree.core.cleanup.cleanup._branch_tree_matches_squash", return_value=False),
            patch("teatree.core.cleanup.cleanup._branch_pr_is_merged", return_value=False),
            pytest.raises(RuntimeError, match="content not upstream"),
        ):
            cleanup_worktree(wt, strict_hygiene=True)
        mock_git.worktree_remove.assert_not_called()

    @_patch_content
    @_patch_overlay
    @_patch_git
    @_patch_config
    def test_non_strict_hygiene_allows_pushed_but_unmerged_branch(
        self,
        mock_config: MagicMock,
        mock_git: MagicMock,
        mock_overlay: MagicMock,
        mock_content: MagicMock,
    ) -> None:
        """Pushed-but-unmerged branch is allowed when strict hygiene is off.

        This is the automated FSM teardown contract — a branch pushed to its
        own remote ref passes; only the data-loss guard still applies. The
        content hygiene gate is skipped entirely, so it is never consulted.
        """
        _mock_workspace(mock_config)
        _no_unpushed(mock_git)
        mock_overlay.return_value.provisioning.cleanup_steps.return_value = []
        mock_git.status_porcelain.return_value = ""
        mock_git.commits_absent_from_all_remotes.return_value = []  # pushed
        mock_git.unsynced_commits.return_value = ["abc123 feat: pushed not merged"]
        mock_content.return_value = ["abc123"]  # would block under strict hygiene

        wt = self._make_worktree(wt_path="/tmp/wt/org/repo")
        cleanup_worktree(wt, strict_hygiene=False)
        mock_git.worktree_remove.assert_called_once()
        mock_content.assert_not_called()

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
        mock_overlay.return_value.provisioning.cleanup_steps.return_value = []
        mock_git.status_porcelain.return_value = ""
        mock_git.unsynced_commits.return_value = []

        wt = self._make_worktree(wt_path="/tmp/wt/org/repo")
        cleanup_worktree(wt)

        mock_git.worktree_remove.assert_called_once()
        mock_git.branch_delete.assert_called_once()


class TestCleanupWorktreeSurvivesMissingProvisionTimebox(TestCase):
    """souliane/teatree#2664 — teardown completes ALL steps on a stale base.

    The benign prek hook-cleanup path (``_remove_git_worktree`` →
    ``prek_hook.remove_stale_hooks`` → ``_shared_hooks_dir`` → ``run_step``)
    drags in a lazy ``import teatree.core.provision.provision_timebox``. When the executing
    checkout's base predates that module the import raised ``ModuleNotFoundError``
    and aborted ``cleanup_worktree`` mid-stream — every step ordered AFTER the
    abort (DB drop, pass-entry removal, ``Worktree`` row delete) was SKIPPED,
    leaving an orphaned DB and DB row. The fix makes the abort impossible, so the
    later steps run.
    """

    def _make_worktree(self, *, db_name: str = "wt_2664") -> Worktree:
        ticket = Ticket.objects.create(
            issue_url="https://gitlab.com/org/repo/-/issues/2664",
            state=Ticket.State.IN_REVIEW,
        )
        return Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="org/repo",
            branch="fix-2664",
            db_name=db_name,
            extra={"worktree_path": "/tmp/wt/org/repo"},
        )

    @_patch_overlay
    @_patch_config
    def test_db_drop_and_row_delete_still_run_when_module_absent(
        self,
        mock_config: MagicMock,
        mock_overlay: MagicMock,
    ) -> None:
        """The prek hook-cleanup import failing must not skip the later teardown steps.

        ``git`` is NOT wholesale-mocked here: the real ``cleanup.git`` runs so
        ``prek_hook.remove_stale_hooks`` genuinely reaches ``run_step`` (the
        abort site) against a tmp worktree path with no checkout. The assertion
        is anti-vacuous — it pins that the DB-drop step IS invoked and the
        ``Worktree`` row IS deleted, the two steps the abort skipped, not merely
        that no exception escaped.
        """
        _mock_workspace(mock_config)
        mock_overlay.return_value.provisioning.cleanup_steps.return_value = []
        mock_overlay.return_value.provisioning.reap_external_resources.return_value = []

        wt = self._make_worktree(db_name="wt_2664")
        wt_id = wt.pk

        with (
            patch("teatree.core.cleanup.cleanup.git") as mock_git,
            patch("teatree.core.cleanup.cleanup.drop_db") as mock_drop,
            patch("teatree.core.runners.worktree_start.docker_compose_down"),
            provision_timebox_unimportable(),
        ):
            _no_unpushed(mock_git)
            mock_git.status_porcelain.return_value = ""
            mock_git.unsynced_commits.return_value = []
            result = cleanup_worktree(wt, strict_hygiene=False)

        mock_drop.assert_called_once()
        assert mock_drop.call_args.args == ("wt_2664",)
        assert not Worktree.objects.filter(pk=wt_id).exists()
        assert result.clean is True


class TestCleanupWorktreeSurvivesVanishedHookPath(TestCase):
    """souliane/teatree#2692 — teardown completes ALL steps when hook-cleanup raises.

    The benign prek hook-cleanup step (``_remove_git_worktree`` →
    ``prek_hook.remove_stale_hooks``) resolves a PATH-hardened hook's relative
    ``PREK="prek"`` value, which raises ``FileNotFoundError`` once the process
    CWD has vanished mid-teardown (the worktree dir was removed earlier in the
    same run). That throw aborted ``cleanup_worktree`` before the DB drop and
    ``Worktree`` row delete — leaving an orphaned DB and DB row. Hook cleanup is
    best-effort: its failure is surfaced, never propagated, so the later steps run.
    """

    def _make_worktree(self, *, db_name: str = "wt_2692") -> Worktree:
        ticket = Ticket.objects.create(
            issue_url="https://gitlab.com/org/repo/-/issues/2692",
            state=Ticket.State.IN_REVIEW,
        )
        return Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="org/repo",
            branch="fix-2692",
            db_name=db_name,
            extra={"worktree_path": "/tmp/wt/org/repo"},
        )

    @_patch_overlay
    @_patch_config
    def test_db_drop_and_row_delete_still_run_when_hook_cleanup_raises(
        self,
        mock_config: MagicMock,
        mock_overlay: MagicMock,
    ) -> None:
        """A ``FileNotFoundError`` from hook cleanup is surfaced, not propagated.

        Anti-vacuous: it pins that the DB-drop step IS invoked and the
        ``Worktree`` row IS deleted (the two steps the abort skipped), and that
        the hook-cleanup failure is recorded in ``errors`` rather than swallowed.
        """
        _mock_workspace(mock_config)
        mock_overlay.return_value.provisioning.cleanup_steps.return_value = []
        mock_overlay.return_value.provisioning.reap_external_resources.return_value = []

        wt = self._make_worktree(db_name="wt_2692")
        wt_id = wt.pk

        with (
            patch("teatree.core.cleanup.cleanup.git") as mock_git,
            patch("teatree.core.cleanup.cleanup.drop_db") as mock_drop,
            patch("teatree.core.runners.worktree_start.docker_compose_down"),
            patch(
                "teatree.core.cleanup.cleanup.prek_hook.remove_stale_hooks",
                side_effect=FileNotFoundError(2, "No such file or directory"),
            ),
        ):
            _no_unpushed(mock_git)
            mock_git.status_porcelain.return_value = ""
            mock_git.unsynced_commits.return_value = []
            result = cleanup_worktree(wt, strict_hygiene=False)

        mock_drop.assert_called_once()
        assert mock_drop.call_args.args == ("wt_2692",)
        assert not Worktree.objects.filter(pk=wt_id).exists()
        assert result.clean is False
        assert any("hook" in e.lower() for e in result.errors)


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
        row and pass entry orphaned) or was swallowed. Now the
        failure is a descriptive ``errors`` entry, the result is non-clean,
        and every other resource is still cleaned.
        """
        _mock_workspace(mock_config)
        _no_unpushed(mock_git)
        mock_overlay.return_value.provisioning.cleanup_steps.return_value = []
        mock_overlay.return_value.config.teardown_removes_pass_entries = True
        mock_git.status_porcelain.return_value = ""
        mock_git.unsynced_commits.return_value = []

        wt = self._make_worktree(db_name="wt_99")
        wt_id = wt.pk
        ticket_id = wt.ticket_id

        with (
            patch(
                "teatree.core.cleanup.cleanup.drop_db",
                side_effect=CommandFailedError(["dropdb", "wt_99"], 1, "", "connection refused"),
            ),
            patch("teatree.core.cleanup.cleanup.remove_postgres_pass_entry") as mock_remove,
        ):
            result = cleanup_worktree(wt)

        # Failure surfaced, not swallowed
        assert result.clean is False
        assert any("wt_99" in e for e in result.errors)
        assert any("connection refused" in e for e in result.errors)
        # Other resources STILL reaped despite the DB-drop failure.
        # Pass key is ticket-pk-scoped (canonical, unique), not ticket_number.
        mock_remove.assert_called_once_with(ticket_id)
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
        mock_overlay.return_value.provisioning.cleanup_steps.return_value = []
        mock_overlay.return_value.config.teardown_removes_pass_entries = True
        mock_git.status_porcelain.return_value = ""
        mock_git.unsynced_commits.return_value = []

        wt = self._make_worktree(db_name="")
        wt_id = wt.pk

        with (
            patch("teatree.core.cleanup.cleanup.drop_db"),
            patch(
                "teatree.core.cleanup.cleanup.remove_postgres_pass_entry",
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
        mock_overlay.return_value.provisioning.cleanup_steps.return_value = [failing_step]
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
        mock_overlay.return_value.provisioning.cleanup_steps.return_value = []
        mock_git.status_porcelain.return_value = ""
        mock_git.unsynced_commits.return_value = []

        wt = self._make_worktree(db_name="")
        with patch("teatree.core.cleanup.cleanup.drop_db"):
            result = cleanup_worktree(wt)

        assert result.clean is True
        assert result.errors == []
        assert "org/repo" in result.label
        assert "org/repo" in str(result)


# ---------------------------------------------------------------------------
# Multi-overlay regression (#295)
# ---------------------------------------------------------------------------


class _NamedOverlayRuntime(OverlayRuntime):
    def run_commands(self, worktree: Worktree) -> RunCommands:
        return {}


class _NamedOverlay(OverlayBase):
    runtime = _NamedOverlayRuntime()
    """Minimal OverlayBase with a string marker so tests can distinguish instances."""

    def __init__(self, marker: str) -> None:
        super().__init__()
        self.marker = marker

    def get_repos(self) -> list[str]:
        return []

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        return []


_OVERLAY_A = "overlay-alpha"
_OVERLAY_B = "overlay-beta"


class TestCleanupWorktreeMultiOverlay(TestCase):
    """Regression: ``cleanup_worktree`` must not call bare ``get_overlay()`` (#295).

    With two overlays installed, ``get_overlay()`` with no name raises
    ``ImproperlyConfigured: Multiple overlays found``.  The fix derives the
    overlay from the worktree's own field via ``get_overlay_for_worktree``.
    """

    @pytest.fixture(autouse=True)
    def _register_both_overlays(self) -> Iterator[None]:
        self.overlay_a = _NamedOverlay(_OVERLAY_A)
        self.overlay_b = _NamedOverlay(_OVERLAY_B)
        registry = {_OVERLAY_A: self.overlay_a, _OVERLAY_B: self.overlay_b}
        with patch("teatree.core.overlay_loader._discover_overlays", return_value=registry):
            yield

    def _worktree(self, *, overlay: str) -> Worktree:
        ticket = Ticket.objects.create(
            overlay=overlay,
            issue_url=f"https://example.com/issues/295-{overlay}",
            state=Ticket.State.IN_REVIEW,
        )
        return Worktree.objects.create(
            ticket=ticket,
            overlay=overlay,
            repo_path="org/repo",
            branch="fix-295",
            extra={"worktree_path": "/tmp/wt/org/repo"},
        )

    @patch("teatree.core.cleanup.cleanup.clone_root")
    @patch("teatree.core.cleanup.cleanup.git")
    def test_cleanup_worktree_resolves_overlay_from_worktree_field(
        self,
        mock_git: MagicMock,
        mock_config: MagicMock,
    ) -> None:
        """``cleanup_worktree`` must not raise when multiple overlays are installed.

        Before #295's fix the bare ``get_overlay()`` call on line 528 of
        ``cleanup.py`` raised ``ImproperlyConfigured: Multiple overlays found``
        as soon as two overlays were in the registry.  After the fix,
        ``get_overlay_for_worktree(worktree)`` is called and the correct
        overlay is selected.
        """
        _mock_workspace(mock_config)
        mock_git.commits_absent_from_all_remotes.return_value = []
        mock_git.status_porcelain.return_value = ""
        mock_git.unsynced_commits.return_value = []

        # cleanup_worktree calls overlay.provisioning.cleanup_steps — wire it on a
        # per-instance facet so overlay_a/overlay_b don't share the class-level default.
        self.overlay_a.provisioning = type(self.overlay_a.provisioning)()
        self.overlay_b.provisioning = type(self.overlay_b.provisioning)()
        self.overlay_a.provisioning.cleanup_steps = lambda wt: []  # type: ignore[method-assign]
        self.overlay_b.provisioning.cleanup_steps = lambda wt: []  # type: ignore[method-assign]
        self.overlay_a.provisioning.reap_external_resources = lambda wt: []  # type: ignore[method-assign]
        self.overlay_b.provisioning.reap_external_resources = lambda wt: []  # type: ignore[method-assign]

        wt = self._worktree(overlay=_OVERLAY_A)
        # Must NOT raise ImproperlyConfigured — and must complete successfully.
        result = cleanup_worktree(wt)
        assert result.clean is True

    @patch("teatree.core.cleanup.cleanup.clone_root")
    @patch("teatree.core.cleanup.cleanup.git")
    def test_cleanup_worktree_selects_correct_overlay(
        self,
        mock_git: MagicMock,
        mock_config: MagicMock,
    ) -> None:
        """The overlay that matches the worktree field is the one whose steps run.

        Each overlay tracks whether its ``provisioning.cleanup_steps`` was called, so the
        test can assert that overlay-B's steps were invoked and overlay-A's were not.
        """
        _mock_workspace(mock_config)
        mock_git.commits_absent_from_all_remotes.return_value = []
        mock_git.status_porcelain.return_value = ""
        mock_git.unsynced_commits.return_value = []

        a_called: list[bool] = []
        b_called: list[bool] = []

        def steps_a(wt: Worktree) -> list:
            a_called.append(True)
            return []

        def steps_b(wt: Worktree) -> list:
            b_called.append(True)
            return []

        self.overlay_a.provisioning = type(self.overlay_a.provisioning)()
        self.overlay_b.provisioning = type(self.overlay_b.provisioning)()
        self.overlay_a.provisioning.cleanup_steps = steps_a  # type: ignore[method-assign]
        self.overlay_b.provisioning.cleanup_steps = steps_b  # type: ignore[method-assign]
        self.overlay_a.provisioning.reap_external_resources = lambda wt: []  # type: ignore[method-assign]
        self.overlay_b.provisioning.reap_external_resources = lambda wt: []  # type: ignore[method-assign]

        wt = self._worktree(overlay=_OVERLAY_B)
        cleanup_worktree(wt)

        assert b_called, "overlay-B's cleanup steps were not invoked"
        assert not a_called, "overlay-A's cleanup steps were invoked but should not have been"


class TestCleanupWorktreeLivenessGuard(TestCase):
    """The funnel liveness guard: an opportunistic teardown never reaps live work.

    ``cleanup_worktree`` is the single seam every teardown caller routes through.
    With ``respect_liveness`` (default on), a worktree under live work — a live
    session, an active/claimed task, an external-delivery lease, a recent E2E
    run, or an explicit pin — raises :class:`WorktreeBusyError` before any
    destructive step. ``force=True`` (abandon) and ``respect_liveness=False``
    (FSM-driven teardown) bypass it. The IRREVERSIBLE teardown therefore never
    protects LESS than the REVERSIBLE idle-stack reaper (#291/#2243).
    """

    def _make_worktree(self) -> Worktree:
        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/2243",
            state=Ticket.State.MERGED,
        )
        return Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="org/repo",
            branch="fix-2243",
            extra={"worktree_path": "/tmp/wt/org/repo"},
        )

    def _tear_down(self, wt: Worktree, **kwargs: object) -> None:
        """Run cleanup_worktree with the git layer mocked so a non-busy teardown completes."""
        with _patch_config as mock_config, _patch_git as mock_git, _patch_overlay as mock_overlay:
            _mock_workspace(mock_config)
            _no_unpushed(mock_git)
            mock_git.status_porcelain.return_value = ""
            mock_overlay.return_value.provisioning.cleanup_steps.return_value = []
            mock_overlay.return_value.provisioning.reap_external_resources.return_value = []
            cleanup_worktree(wt, **kwargs)

    def test_live_session_keeps_worktree_by_default(self) -> None:
        wt = self._make_worktree()
        Session.objects.create(ticket=wt.ticket, overlay="test")  # live: ended_at is null

        with pytest.raises(WorktreeBusyError, match="live work"):
            cleanup_worktree(wt)

        assert Worktree.objects.filter(pk=wt.pk).exists(), "DATA LOSS: busy worktree reaped"

    def test_claimed_task_keeps_worktree_by_default(self) -> None:
        wt = self._make_worktree()
        session = Session.objects.create(ticket=wt.ticket, overlay="test")
        session.ended_at = timezone.now()
        session.save(update_fields=["ended_at"])
        Task.objects.create(ticket=wt.ticket, session=session, status=Task.Status.CLAIMED)

        with pytest.raises(WorktreeBusyError, match="live work"):
            cleanup_worktree(wt)

    def test_external_delivery_lease_keeps_worktree(self) -> None:
        """A worktree under a live external-delivery lease is KEPT — the wider predicate (#2227)."""
        wt = self._make_worktree()
        mark_external_delivery(wt.ticket)

        with pytest.raises(WorktreeBusyError, match="live work"):
            cleanup_worktree(wt)

    def test_recent_e2e_run_keeps_worktree(self) -> None:
        wt = self._make_worktree()
        wt.last_e2e_run = timezone.now()
        wt.save(update_fields=["last_e2e_run"])

        with pytest.raises(WorktreeBusyError, match="live work"):
            cleanup_worktree(wt)

    def test_reaper_pinned_keeps_worktree(self) -> None:
        wt = self._make_worktree()
        wt.extra = {**wt.extra, "reaper_pinned": True}
        wt.save(update_fields=["extra"])

        with pytest.raises(WorktreeBusyError, match="live work"):
            cleanup_worktree(wt)

    def test_force_tears_down_busy_worktree(self) -> None:
        """Explicit abandon (force=True) overrides the liveness guard."""
        wt = self._make_worktree()
        Session.objects.create(ticket=wt.ticket, overlay="test")

        self._tear_down(wt, force=True)

        assert not Worktree.objects.filter(pk=wt.pk).exists(), "force=True must still tear down"

    def test_respect_liveness_false_tears_down_busy_worktree(self) -> None:
        """FSM-driven teardown (respect_liveness=False) bypasses the guard — no leak regression."""
        wt = self._make_worktree()
        Session.objects.create(ticket=wt.ticket, overlay="test")

        self._tear_down(wt, respect_liveness=False)

        assert not Worktree.objects.filter(pk=wt.pk).exists(), "FSM teardown of a merged ticket must proceed"

    def test_idle_worktree_is_torn_down(self) -> None:
        """Safe-reap preserved: a worktree with no live work tears down as before."""
        wt = self._make_worktree()

        self._tear_down(wt)

        assert not Worktree.objects.filter(pk=wt.pk).exists(), "idle worktree should still reap"
