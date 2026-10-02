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


class TestMethodIsClassifiedPerApiSegment:
    """The HTTP method is read from the ``glab api`` segment, never from the rest of the Bash call.

    Observed: read-only ``…/notes`` / ``…/discussions`` GETs were denied as implicit
    POSTs because a ``-f`` / ``-F`` in ANOTHER segment of the same call (``pgrep -f``,
    ``rm -f``, ``glab repo view -F json``) matched the body-flag regex that scanned the
    whole command text.
    """

    @pytest.mark.parametrize(
        "command",
        [
            (
                'glab api "/projects/1/merge_requests/6898/notes?per_page=10" 2>/dev/null\n'
                'pgrep -f "t3 review" >/dev/null'
            ),
            (
                'glab api "projects/1/merge_requests/8075/notes?per_page=100" 2>/dev/null | python3 -c "print(1)"\n'
                "rm -f /tmp/x.md"
            ),
            'glab api "projects/1/merge_requests/6/discussions/abc" > /tmp/t.json; command rm -f /tmp/t.json',
            (
                "PID=$(glab repo view org/repo -F json | jq .id); "
                'glab api "projects/$PID/issues/4680/notes?per_page=100"'
            ),
            (
                'glab api "projects/1/merge_requests/7/discussions?per_page=100" 2>/dev/null || '
                'glab api "projects/1/merge_requests/7"'
            ),
            (
                "glab api projects/1/merge_requests/8018/discussions --paginate --output ndjson > /tmp/d.ndjson; "
                "grep -F needle /tmp/d.ndjson"
            ),
        ],
    )
    def test_a_read_next_to_an_unrelated_body_flag_is_allowed(self, command: str) -> None:
        assert is_raw_review_write(command) is False
        assert raw_review_deny_reason(command) is None

    @pytest.mark.parametrize(
        "command",
        [
            (
                "glab api projects/1/merge_requests/7/discussions -X POST -f body=hi; "
                'glab api "projects/1/merge_requests/7"'
            ),
            'glab api "projects/1/merge_requests/7" && glab api projects/1/merge_requests/7/notes -f body=hi',
            "rm -f /tmp/x; glab api projects/1/merge_requests/7/notes --method POST --field body=@/tmp/b.md",
        ],
    )
    def test_a_write_in_its_own_segment_is_still_denied(self, command: str) -> None:
        assert is_raw_review_write(command) is True
        assert raw_review_deny_reason(command) is not None

    @pytest.mark.parametrize(
        "command",
        [
            'U=projects/1/merge_requests/7/notes; glab api "$U" -X POST -f body=hi',
            'URL=projects/1/merge_requests/7/notes; glab api "$URL" --field body=@/tmp/b.md',
            ("URL=$(printf 'projects/1/merge_requests/7/notes'); glab api \"$URL\" --method POST --field body=hi"),
        ],
    )
    def test_an_endpoint_carried_in_a_shell_variable_from_a_prior_segment_is_still_denied(self, command: str) -> None:
        """The endpoint pattern is checked on the WHOLE command (not per-segment).

        A prior segment assigning ``URL=<endpoint>`` and a later segment posting to
        ``"$URL"`` never puts the literal endpoint text in the SAME segment as the
        write — a per-segment endpoint check misses it entirely. Only the HTTP-METHOD
        classification is legitimately per-segment (the fix for ``-f``/``-F``
        belonging to an unrelated command elsewhere in the call).
        """
        assert is_raw_review_write(command) is True
        assert raw_review_deny_reason(command) is not None


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
