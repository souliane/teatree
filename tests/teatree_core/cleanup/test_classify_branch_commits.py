"""``classify_branch_commits`` bucketing logic.

Split verbatim from the former monolithic ``tests/teatree_core/test_cleanup.py``
(souliane/teatree#443). These pure-logic cases drive the classifier directly
with a mocked ``teatree.core.cleanup.git.run_strict`` (#2937: the classifier's
git calls fail loud on a real git error, so the happy-path mock target moved
from the lenient ``git.run`` to the strict runner); no bucketing behavior
change.
"""

from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase

from teatree.core.cleanup import BranchClassification, BranchCommit, classify_branch_commits


class TestClassifyBranchCommits(TestCase):
    """``classify_branch_commits`` sorts branch-local commits into three buckets.

    The classifier is the foundation for squash-merge-aware cleanup: it lets
    the caller distinguish content already on the default branch (under a new
    SHA, via squash-merge) from work that still needs pushing.
    """

    @patch("teatree.core.cleanup.git.run_strict")
    def test_empty_when_no_unsynced_commits(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = ["", ""]  # unsynced log empty, target log empty
        result = classify_branch_commits("/repo", "feature")
        assert result == BranchClassification(squash_merged=[], merge_commits=[], genuinely_ahead=[])

    @patch("teatree.core.cleanup.git.run_strict")
    def test_subject_match_with_pr_suffix_marks_squash_merged(self, mock_run: MagicMock) -> None:
        branch_log = "abc123\x00parent1\x00fix(ui): button alignment"
        target_log = "fix(ui): button alignment (#42)\nfeat(core): unrelated"
        mock_run.side_effect = [branch_log, target_log]

        result = classify_branch_commits("/repo", "feature")

        assert result.squash_merged == [BranchCommit(sha="abc123", subject="fix(ui): button alignment", is_merge=False)]
        assert result.genuinely_ahead == []

    @patch("teatree.core.cleanup.git.run_strict")
    def test_strips_type_prefix_for_relax_to_feat_rewrite(self, mock_run: MagicMock) -> None:
        """Branch has ``relax: X (#140)``; main has ``feat(fsm): X (#140) (#368)`` — same content, different prefix."""
        branch_log = "def456\x00parent1\x00relax: transition-driven workflow (#140)"
        target_log = "feat(fsm): transition-driven workflow (#140) (#368)"
        mock_run.side_effect = [branch_log, target_log]

        result = classify_branch_commits("/repo", "feature")

        assert len(result.squash_merged) == 1
        assert result.squash_merged[0].sha == "def456"
        assert result.genuinely_ahead == []

    @patch("teatree.core.cleanup.git.run_strict")
    def test_merge_commit_detected_via_multiple_parents(self, mock_run: MagicMock) -> None:
        branch_log = "mrg001\x00parent1 parent2\x00Merge branch 'main' into feature"
        target_log = ""
        mock_run.side_effect = [branch_log, target_log]

        result = classify_branch_commits("/repo", "feature")

        assert len(result.merge_commits) == 1
        assert result.merge_commits[0].is_merge is True
        assert result.genuinely_ahead == []
        assert result.squash_merged == []

    @patch("teatree.core.cleanup.git.run_strict")
    def test_genuinely_ahead_when_no_subject_match(self, mock_run: MagicMock) -> None:
        branch_log = "new001\x00parent1\x00fix(hooks): strip trailing whitespace"
        target_log = "chore(deps): bump pytest\nfeat(config): add t3.mode"
        mock_run.side_effect = [branch_log, target_log]

        result = classify_branch_commits("/repo", "feature")

        assert result.squash_merged == []
        assert len(result.genuinely_ahead) == 1
        assert result.genuinely_ahead[0].sha == "new001"

    @patch("teatree.core.cleanup.git.run_strict")
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

    @patch("teatree.core.cleanup.git.run_strict")
    def test_unsynced_fully_merged_via_squash_returns_empty_genuinely_ahead(self, mock_run: MagicMock) -> None:
        """Every unsynced commit has a subject match on target → branch is safe to clean."""
        branch_log = "sha1\x00p1\x00feat(config): generic per-overlay override\nsha2\x00p1\x00fix: trailing whitespace"
        target_log = "feat(config): generic per-overlay override (#375)\nfix: trailing whitespace (#200)"
        mock_run.side_effect = [branch_log, target_log]

        result = classify_branch_commits("/repo", "feature")

        assert result.genuinely_ahead == []
        assert len(result.squash_merged) == 2

    @patch("teatree.core.cleanup.git.run_strict")
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

    @patch("teatree.core.cleanup.git.run_strict")
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

    @patch("teatree.core.cleanup.git.run_strict")
    def test_plain_subjects_without_release_note_suffix_still_match(self, mock_run: MagicMock) -> None:
        """Fallback case — neither title has a release-note suffix (e.g. ``chore:`` without ticket)."""
        branch_log = "sha1\x00p1\x00chore(prek): move pip-audit to manual stage"
        target_log = "chore(prek): move pip-audit to manual stage (#383)"
        mock_run.side_effect = [branch_log, target_log]

        result = classify_branch_commits("/repo", "feature")

        assert [c.sha for c in result.squash_merged] == ["sha1"]
        assert result.genuinely_ahead == []

    @patch("teatree.core.cleanup.git.run_strict")
    def test_git_failure_on_unsynced_log_propagates_instead_of_empty_classification(
        self,
        mock_run: MagicMock,
    ) -> None:
        """#2937: a git failure must raise, never silently look like "no unsynced commits"."""
        from teatree.utils.run import CommandFailedError  # noqa: PLC0415

        mock_run.side_effect = CommandFailedError(
            cmd=["git", "-C", "owner/repo", "log", "feature", "--not", "origin/main"],
            returncode=128,
            stdout="",
            stderr="fatal: cannot change to 'owner/repo': No such file or directory",
        )

        with pytest.raises(CommandFailedError):
            classify_branch_commits("owner/repo", "feature")
