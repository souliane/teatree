"""``_branch_pr_is_merged`` — the canonical forge merged-PR probe (#1578).

The fallback the residual-worktree reaper consults when subject-matching and
the squash-tree heuristic both break down on a long-diverged branch. It must
answer ``True`` only on a positive merged signal from the forge and fail safe
to ``False`` on every uncertainty (no merged PR, CLI missing, parse error) so
the conservative refuse-and-report stands. The subprocess CLI is mocked exactly
like :func:`is_squash_merged`'s tests in ``test_workspace.py``.
"""

import subprocess
from unittest.mock import patch

from django.test import TestCase

from teatree.core.worktree import branch_classification
from teatree.core.worktree.branch_classification import _branch_pr_is_merged
from teatree.forge_credentials import ForgeTokenResolution, ForgeTokenState


class TestBranchPrIsMerged(TestCase):
    def setUp(self) -> None:
        branch_classification._branch_pr_is_merged.cache_clear()

        def _route(_repo: str, *, credential: str) -> ForgeTokenResolution:
            return ForgeTokenResolution(credential, "test", ForgeTokenState.TOKEN, token="routed")

        self.route = patch(
            "teatree.core.forge_pr_probe.resolve_repo_token",
            side_effect=_route,
        )
        self.route.start()
        self.addCleanup(self.route.stop)
        self.addCleanup(branch_classification._branch_pr_is_merged.cache_clear)

    def test_true_when_github_reports_merged_pr(self) -> None:
        with (
            patch.object(branch_classification, "forge_for_repo", return_value="github"),
            patch(
                "teatree.utils.run.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout='[{"number":7}]'),
            ),
        ):
            assert _branch_pr_is_merged("/repo", "1206-feat-review-run") is True

    def test_true_when_gitlab_reports_merged_mr(self) -> None:
        with (
            patch.object(branch_classification, "forge_for_repo", return_value="gitlab"),
            patch(
                "teatree.utils.run.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout='[{"iid":5,"merge_commit_sha":"abc"}]'),
            ),
        ):
            assert _branch_pr_is_merged("/repo", "s-repo-99-fix") is True

    def test_false_when_matching_forge_has_no_merged_pr(self) -> None:
        for forge in ("github", "gitlab"):
            branch_classification._branch_pr_is_merged.cache_clear()
            with (
                self.subTest(forge=forge),
                patch.object(branch_classification, "forge_for_repo", return_value=forge),
                patch(
                    "teatree.utils.run.subprocess.run",
                    return_value=subprocess.CompletedProcess([], 0, stdout="[]"),
                ),
            ):
                assert _branch_pr_is_merged("/repo", "1234-feat-pending") is False

    def test_false_when_host_cli_is_missing_or_blocked(self) -> None:
        for exc in (FileNotFoundError("gh"), PermissionError("blocked")):
            branch_classification._branch_pr_is_merged.cache_clear()
            with (
                patch.object(branch_classification, "forge_for_repo", return_value="github"),
                patch("teatree.utils.run.subprocess.run", side_effect=exc),
            ):
                assert _branch_pr_is_merged("/repo", "1234-feat-pending") is False

    def test_false_when_payload_is_unparsable(self) -> None:
        with (
            patch.object(branch_classification, "forge_for_repo", return_value="github"),
            patch(
                "teatree.utils.run.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="not-json"),
            ),
        ):
            assert _branch_pr_is_merged("/repo", "1234-feat-pending") is False
