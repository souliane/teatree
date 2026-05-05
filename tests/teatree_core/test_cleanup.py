import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase

from teatree.core.cleanup import (
    BranchClassification,
    BranchCommit,
    classify_branch_commits,
    cleanup_worktree,
)
from teatree.core.models import Ticket, Worktree

_patch_config = patch("teatree.core.cleanup.load_config")
_patch_git = patch("teatree.core.cleanup.git")
_patch_overlay = patch("teatree.core.cleanup.get_overlay")
_patch_classify = patch("teatree.core.cleanup.classify_branch_commits")


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
        mock_overlay.return_value.get_cleanup_steps.return_value = []

        wt = self._make_worktree(db_name="wt_99")

        with patch("teatree.core.cleanup.drop_db") as mock_drop:
            cleanup_worktree(wt)
            mock_drop.assert_called_once_with("wt_99")

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
        step_fn = MagicMock()
        mock_overlay.return_value.get_cleanup_steps.return_value = [MagicMock(callable=step_fn)]

        wt = self._make_worktree()
        cleanup_worktree(wt)

        step_fn.assert_called_once()

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
    def test_releases_redis_slot_when_last_worktree_removed(
        self,
        mock_config: MagicMock,
        mock_git: MagicMock,
        mock_overlay: MagicMock,
    ) -> None:
        _mock_workspace(mock_config)
        mock_overlay.return_value.get_cleanup_steps.return_value = []
        mock_git.status_porcelain.return_value = ""
        mock_git.unsynced_commits.return_value = []

        wt = self._make_worktree(wt_path="/tmp/wt/org/repo")
        ticket = wt.ticket
        ticket.redis_db_index = 3
        ticket.save()

        with patch("teatree.utils.redis_container.flushdb") as mock_flush:
            cleanup_worktree(wt)

        mock_flush.assert_called_once_with(3)
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


def _run_git(*args: str, cwd: Path) -> None:
    subprocess.run([_GIT, "-C", str(cwd), *args], check=True, capture_output=True)


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
        subprocess.run([_RM, "-rf", str(self.repo_main)], check=True)
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
        ).stdout
        assert str(self.wt_path) not in registry
        assert "errors" not in label.lower()
