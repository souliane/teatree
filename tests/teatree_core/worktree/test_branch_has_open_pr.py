"""``_branch_has_open_pr`` — the canonical forge open-PR probe (#3093).

The veto the squash-merged done signal consults before trusting the content
heuristic: a branch whose tip is patch-id-equivalent to ``origin/<default>`` but
whose PR is still OPEN must not be reported ``done (squash-merged)``. It answers
``True`` only on a positive open-PR signal and fails safe to ``False`` on every
uncertainty (no open PR, CLI missing, parse error) so the additive keep only ever
adds safety, never disables a real cleanup. The subprocess CLI is mocked exactly
like :func:`_branch_pr_is_merged`'s tests.
"""

import subprocess
from collections.abc import Callable
from unittest.mock import patch

from django.test import TestCase

from teatree.core.worktree.branch_classification import _branch_has_open_pr


def _dispatch(gh_stdout: str, glab_stdout: str) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Return mock stdout for ``gh`` vs ``glab`` based on the invoked binary."""

    def _side_effect(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        cmd = args[0] if args else ""
        return subprocess.CompletedProcess([], 0, stdout=gh_stdout if cmd == "gh" else glab_stdout)

    return _side_effect


class TestBranchHasOpenPr(TestCase):
    def test_true_when_github_reports_open_pr(self) -> None:
        with patch(
            "teatree.utils.run.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, stdout='[{"number":7}]'),
        ):
            assert _branch_has_open_pr("/repo", "1206-feat-review-run") is True

    def test_true_when_gitlab_reports_open_mr(self) -> None:
        with patch(
            "teatree.utils.run.subprocess.run",
            side_effect=_dispatch(gh_stdout="[]", glab_stdout='[{"iid":5,"web_url":"u"}]'),
        ):
            assert _branch_has_open_pr("/repo", "s-repo-99-fix") is True

    def test_false_when_no_open_pr_on_either_forge(self) -> None:
        with patch(
            "teatree.utils.run.subprocess.run",
            side_effect=_dispatch(gh_stdout="[]", glab_stdout="[]"),
        ):
            assert _branch_has_open_pr("/repo", "1234-feat-merged") is False

    def test_false_when_host_cli_is_missing_or_blocked(self) -> None:
        for exc in (FileNotFoundError("gh"), PermissionError("blocked")):
            with patch("teatree.utils.run.subprocess.run", side_effect=exc):
                assert _branch_has_open_pr("/repo", "1234-feat-merged") is False

    def test_false_when_payload_is_unparsable(self) -> None:
        with patch(
            "teatree.utils.run.subprocess.run",
            side_effect=_dispatch(gh_stdout="not-json", glab_stdout="also-not-json"),
        ):
            assert _branch_has_open_pr("/repo", "1234-feat-merged") is False
