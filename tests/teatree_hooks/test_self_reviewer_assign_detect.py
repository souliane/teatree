"""The self-reviewer-assign Bash detector leaf — mirrors the PreToolUse guard's matcher."""

import pytest

from teatree.hooks.self_reviewer_assign_detect import bash_assigns_reviewer, reviewer_assign_deny_reason


class TestDenies:
    @pytest.mark.parametrize(
        "command",
        [
            "glab mr update 7 --reviewer alice",
            "glab mr create --reviewers alice,bob",
            "gh pr create --reviewer bob",
            "gh pr create -r bob",
            "gh pr edit 7 --add-reviewer bob",
            "glab api projects/1/merge_requests/2 -X PUT -f reviewer_ids=3",
            "gh api repos/o/x/pulls/7/requested_reviewers -f reviewers=bob",  # body flag → implicit write
        ],
    )
    def test_reviewer_assignments_are_denied(self, command: str) -> None:
        assert bash_assigns_reviewer(command) is True
        reason = reviewer_assign_deny_reason(command)
        assert reason is not None
        assert "BLOCKED" in reason


class TestAllows:
    @pytest.mark.parametrize(
        "command",
        [
            "gh api repos/o/x/pulls/7/requested_reviewers",  # GET reads the list
            "glab mr update 7 --title 'new title'",  # not a reviewer op
            "gh pr view 7",
            "git commit -m 'note about --reviewer flag'",  # phrase inside a quoted message
            "glab mr merge 7",
            "",
        ],
    )
    def test_reads_and_non_reviewer_ops_pass(self, command: str) -> None:
        assert bash_assigns_reviewer(command) is False
        assert reviewer_assign_deny_reason(command) is None
