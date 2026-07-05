"""The raw-review-post detector leaf — mirrors the PreToolUse guard's matcher."""

import pytest

from teatree.hooks.raw_review_post_detect import is_raw_review_write, raw_review_deny_reason


class TestDenies:
    @pytest.mark.parametrize(
        "command",
        [
            "glab api projects/42/merge_requests/7/discussions -X POST -f body=hi",
            "gh api repos/o/x/pulls/7/comments -X POST -f body=hi",
            "glab api projects/42/merge_requests/7/notes -f body=hi",  # body flag → implicit POST
            "gh api repos/o/x/issues/7/comments -X GET -X POST",  # last-wins POST
            "glab  api projects/42/merge_requests/7/discussions -X POST -f body=hi",  # double space
        ],
    )
    def test_review_writes_are_denied(self, command: str) -> None:
        assert is_raw_review_write(command) is True
        reason = raw_review_deny_reason(command)
        assert reason is not None
        assert "BLOCKED" in reason


class TestAllows:
    @pytest.mark.parametrize(
        "command",
        [
            "glab api projects/42/merge_requests/7/discussions",  # bare GET
            "gh api repos/o/x/pulls/7/comments -X GET",  # explicit GET
            "gh api repos/o/x/pulls/7/comments -X POST -X GET",  # last-wins GET
            "gh api repos/o/x/pulls/7/reviews -X POST -f body=hi",  # not a comment endpoint
            "gh pr view 7",  # not an api command
            "",
        ],
    )
    def test_reads_and_non_review_endpoints_pass(self, command: str) -> None:
        assert is_raw_review_write(command) is False
        assert raw_review_deny_reason(command) is None
