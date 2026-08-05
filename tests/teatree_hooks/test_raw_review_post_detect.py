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


class TestRemedyAddressesTheBlockedObject:
    """The named remedy must be a command that works on the surface the caller addressed.

    A blocked ISSUE/work-item note pointed at ``review post-comment``, which takes an
    integer MR IID and posts to ``merge_requests/<iid>/notes`` — it cannot address an
    issue at all, so the gate blocked and misdirected. The sanctioned create-note-on-issue
    path is ``t3 <overlay> ticket comment``.
    """

    @pytest.mark.parametrize(
        "command",
        [
            "glab api projects/42/issues/8568/notes --method POST --field body=hi",
            "gh api repos/o/x/issues/12/comments -X POST -f body=hi",
        ],
    )
    def test_issue_note_remedy_names_ticket_comment(self, command: str) -> None:
        reason = raw_review_deny_reason(command)
        assert reason is not None
        assert "ticket comment" in reason
        assert "delete-issue-note" in reason

    @pytest.mark.parametrize(
        "command",
        [
            "glab api projects/42/issues/8568/notes --method POST --field body=hi",
            "gh api repos/o/x/issues/12/comments -X POST -f body=hi",
        ],
    )
    def test_issue_note_remedy_does_not_send_the_caller_to_the_mr_only_cli(self, command: str) -> None:
        reason = raw_review_deny_reason(command)
        assert reason is not None
        assert "review post-comment` (draft by default" not in reason
        assert "post-draft-note" not in reason

    @pytest.mark.parametrize(
        "command",
        [
            "glab api projects/42/merge_requests/7/discussions -X POST -f body=hi",
            "gh api repos/o/x/pulls/7/comments -X POST -f body=hi",
        ],
    )
    def test_mr_remedy_still_names_the_review_clis(self, command: str) -> None:
        reason = raw_review_deny_reason(command)
        assert reason is not None
        assert "review post-comment" in reason
        assert "post-draft-note" in reason
        assert "delete-discussion" in reason
        assert "ticket comment" not in reason
