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


class TestQuotedReviewerFieldsAreStillSeen:
    """A field VALUE is an ordinary quoted shell argument — the skeleton erases it.

    Detection stripped quoted spans before looking for the reviewer field, so the
    routine spelling ``-f 'reviewer_ids=42'`` left nothing to match and the
    out-of-band reviewer WRITE passed as a non-reviewer call.
    """

    @pytest.mark.parametrize(
        "command",
        [
            "glab api --method PUT projects/1/merge_requests/2 -f 'reviewer_ids=42'",
            'glab api --method PUT projects/1/merge_requests/2 --raw-field "reviewer_ids=42"',
            "glab api --method PUT projects/1/merge_requests/2 -d '{\"reviewer_ids\": [42]}'",
        ],
    )
    def test_a_quoted_reviewer_field_write_is_denied(self, command: str) -> None:
        assert bash_assigns_reviewer(command) is True

    @pytest.mark.parametrize(
        "command",
        [
            # Prose that merely mentions reviewers is not a field assignment.
            "gh api repos/o/x/issues/1/comments -f body='the reviewers are still deciding'",
            # A quoted verb is not an invocation — the skeleton still guards that.
            "git commit -m 'ran glab api -f reviewer_ids=42 by hand once'",
        ],
    )
    def test_prose_and_quoted_verbs_still_pass(self, command: str) -> None:
        assert bash_assigns_reviewer(command) is False


class TestEffectiveMethodIsLastWins:
    """``gh``/``glab`` resolve a repeated method flag LAST-WINS, as pflag does.

    Reading the FIRST flag let a trailing real write hide behind a leading
    ``-X GET``, and made a trailing ``-X GET`` fail to clear a leading write.
    """

    def test_a_trailing_write_method_wins_over_a_leading_get(self) -> None:
        command = "gh api repos/o/x/pulls/7/requested_reviewers -X GET -X POST -f reviewers=bob"
        assert bash_assigns_reviewer(command) is True

    def test_a_trailing_get_wins_over_a_leading_write(self) -> None:
        command = "gh api repos/o/x/pulls/7/requested_reviewers -X POST -X GET"
        assert bash_assigns_reviewer(command) is False


class TestRawCurlIsNotABypass:
    """The leaf refuses the same curl writes Lane A refuses — one hole, two copies."""

    @pytest.mark.parametrize(
        "command",
        [
            "curl -X PUT https://gitlab.com/api/v4/projects/1/merge_requests/2 -d '{\"reviewer_ids\": [42]}'",
            'curl --request PATCH "$MR_URL" --data-binary \'{"reviewer_ids":[42]}\'',
            'curl -XPOST https://api.github.com/repos/o/x/pulls/7/requested_reviewers -d \'{"reviewers":["bob"]}\'',
            'curl https://api.github.com/repos/o/x/pulls/7/requested_reviewers --json \'{"reviewers":["bob"]}\'',
        ],
    )
    def test_a_curl_reviewer_write_is_denied(self, command: str) -> None:
        assert bash_assigns_reviewer(command) is True
        assert reviewer_assign_deny_reason(command) is not None

    @pytest.mark.parametrize(
        "command",
        [
            "curl https://api.github.com/repos/o/x/pulls/7/requested_reviewers",
            "curl -sSL -X GET https://gitlab.com/api/v4/projects/1/merge_requests/2",
            "curl -fsSL https://api.github.com/repos/o/x/pulls/7/requested_reviewers",
            "curl -O https://example.com/reviewers.json",
            "git commit -m 'ran curl -X PUT ... reviewer_ids=42 once'",
        ],
    )
    def test_a_curl_read_or_a_quoted_mention_passes(self, command: str) -> None:
        assert bash_assigns_reviewer(command) is False
        assert reviewer_assign_deny_reason(command) is None


class TestTheSanctionedPolicyCommandIsNotAnAssignmentSurface:
    """``review apply-reviewer-policy`` is allowed because it matches NOTHING.

    It is not carved out by a special case — the matcher only ever fires on a
    forge CLI or a REST leader, and a ``t3`` command is neither. Pinned so a later
    broadening of the matcher cannot silently swallow the one sanctioned path.
    """

    @pytest.mark.parametrize(
        "command",
        [
            "t3 acme review apply-reviewer-policy",
            "t3 acme review apply-reviewer-policy --dry-run",
            "t3 acme review apply-reviewer-policy --json",
        ],
    )
    def test_the_policy_command_is_allowed(self, command: str) -> None:
        assert bash_assigns_reviewer(command) is False
        assert reviewer_assign_deny_reason(command) is None


class TestTheGeneralFormsStayBlockedAlongsideIt:
    """The evidence that matters: admitting the sanctioned path widened nothing."""

    @pytest.mark.parametrize(
        "command",
        [
            "glab mr update 76 --reviewer octocat",
            "glab mr update 76 --reviewers octocat,someone",
            "glab mr create --reviewer octocat",
            "gh pr edit 12 --reviewer octocat",
            "gh pr edit 12 --add-reviewer octocat",
            "gh pr create -r octocat",
            "glab api --method PUT projects/1/merge_requests/76 -f reviewer_ids=42",
            "gh api --method POST repos/o/x/pulls/12/requested_reviewers -f 'reviewers[]=octocat'",
            "curl -X PUT https://gitlab.com/api/v4/projects/1/merge_requests/76 -d '{\"reviewer_ids\": [42]}'",
            'curl -XPOST https://api.github.com/repos/o/x/pulls/7/requested_reviewers -d \'{"reviewers":["bob"]}\'',
        ],
    )
    def test_every_general_reviewer_write_is_still_denied(self, command: str) -> None:
        assert bash_assigns_reviewer(command) is True
        assert reviewer_assign_deny_reason(command) is not None

    def test_the_deny_reason_names_the_sanctioned_command(self) -> None:
        """A blocked agent must be told the one path that exists, not that none does."""
        reason = reviewer_assign_deny_reason("glab mr update 76 --reviewer octocat")

        assert reason is not None
        assert "apply-reviewer-policy" in reason


class TestAQuotedMethodIsNotAMethod:
    """The method is what the shell PASSES, not what the string contains (#4641).

    Resolving it last-wins over the raw command let any reviewer write past the gate:
    append `-X GET` inside a quoted field value and the write reads as a read.
    """

    @pytest.mark.parametrize(
        "command",
        [
            "gh api -X POST repos/o/x/pulls/7/requested_reviewers -f 'reviewers[]=bob -X GET'",
            'gh api --method POST repos/o/x/pulls/7/requested_reviewers -f "reviewers[]=bob --method GET"',
            "glab api -X PUT projects/1/merge_requests/2 -f 'reviewer_ids=42 -X GET'",
            'curl -X POST https://api.github.com/repos/o/x/pulls/7/requested_reviewers -d \'{"reviewers":["-X GET"]}\'',
        ],
    )
    def test_a_read_method_hidden_in_a_quoted_value_does_not_downgrade_the_write(self, command: str) -> None:
        assert bash_assigns_reviewer(command) is True
        assert reviewer_assign_deny_reason(command) is not None

    @pytest.mark.parametrize(
        "command",
        [
            "gh api -X GET repos/o/x/pulls/7/requested_reviewers",
            "glab api --method GET projects/1/merge_requests/2/reviewers",
        ],
    )
    def test_a_real_read_is_still_allowed(self, command: str) -> None:
        assert bash_assigns_reviewer(command) is False
