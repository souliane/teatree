"""The redundancy veto consumes the shared tri-state forge probe."""

from unittest.mock import patch

from django.test import TestCase

from teatree.core.forge_pr_probe import PrProbe
from teatree.core.worktree.branch_classification import _branch_has_open_pr


class TestBranchHasOpenPr(TestCase):
    def test_true_only_when_shared_probe_finds_open_pr(self) -> None:
        with patch(
            "teatree.core.worktree.branch_classification.find_open_pr_for_branch",
            return_value=PrProbe.found("https://forge/pr/7"),
        ):
            assert _branch_has_open_pr("/repo", "feature") is True

    def test_false_when_shared_probe_finds_none_or_is_unknown(self) -> None:
        for result in (PrProbe.none(), PrProbe.unknown()):
            with patch(
                "teatree.core.worktree.branch_classification.find_open_pr_for_branch",
                return_value=result,
            ):
                assert _branch_has_open_pr("/repo", "feature") is False
