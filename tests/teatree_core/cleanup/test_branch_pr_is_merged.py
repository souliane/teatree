"""``_branch_pr_is_merged`` — the canonical forge merged-PR probe (#1578).

The fallback the residual-worktree reaper consults when subject-matching and
the squash-tree heuristic both break down on a long-diverged branch. It must
answer ``True`` only on a positive merged signal from the forge and fail safe
to ``False`` on every uncertainty (no merged PR, CLI missing, parse error) so
the conservative refuse-and-report stands. The subprocess CLI is mocked exactly
like :func:`is_squash_merged`'s tests in ``test_workspace.py``.
"""

import subprocess
from collections.abc import Callable
from unittest.mock import patch

from django.test import TestCase

from teatree.core.cleanup.cleanup import _branch_pr_is_merged


def _dispatch(gh_stdout: str, glab_stdout: str) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Return mock stdout for ``gh`` vs ``glab`` based on the invoked binary."""

    def _side_effect(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        cmd = args[0] if args else ""
        return subprocess.CompletedProcess([], 0, stdout=gh_stdout if cmd == "gh" else glab_stdout)

    return _side_effect


class TestBranchPrIsMerged(TestCase):
    def test_true_when_github_reports_merged_pr(self) -> None:
        with patch(
            "teatree.utils.run.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, stdout='[{"number":7}]'),
        ):
            assert _branch_pr_is_merged("/repo", "1206-feat-review-run") is True

    def test_true_when_gitlab_reports_merged_mr(self) -> None:
        with patch(
            "teatree.utils.run.subprocess.run",
            side_effect=_dispatch(gh_stdout="[]", glab_stdout='[{"iid":5,"merge_commit_sha":"abc"}]'),
        ):
            assert _branch_pr_is_merged("/repo", "s-repo-99-fix") is True

    def test_false_when_no_merged_pr_on_either_forge(self) -> None:
        with patch(
            "teatree.utils.run.subprocess.run",
            side_effect=_dispatch(gh_stdout="[]", glab_stdout="[]"),
        ):
            assert _branch_pr_is_merged("/repo", "1234-feat-pending") is False

    def test_false_when_host_cli_is_missing_or_blocked(self) -> None:
        for exc in (FileNotFoundError("gh"), PermissionError("blocked")):
            with patch("teatree.utils.run.subprocess.run", side_effect=exc):
                assert _branch_pr_is_merged("/repo", "1234-feat-pending") is False

    def test_false_when_payload_is_unparsable(self) -> None:
        with patch(
            "teatree.utils.run.subprocess.run",
            side_effect=_dispatch(gh_stdout="not-json", glab_stdout="also-not-json"),
        ):
            assert _branch_pr_is_merged("/repo", "1234-feat-pending") is False
