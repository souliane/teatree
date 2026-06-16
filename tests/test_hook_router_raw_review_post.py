"""Tests for the raw-review-post deny gate in hook_router (#1164).

Sub-agents have repeatedly posted MR/PR review comments by shelling out to a
raw forge REST POST (``glab api .../merge_requests/<n>/discussions -X POST``,
``.../notes``, or the GitHub ``.../pulls/<n>/comments``), bypassing the
sanctioned ``t3 <overlay> review post-comment`` / ``post-draft-note`` path
(draft-default + dedup + on-behalf approval). This gate HARD-DENIES those
writes at the Bash boundary while letting plain GET reads through.

The gate is conservative: it denies ONLY clear review-write POSTs and never a
bare read or a non-review endpoint, so a no-false-deny guard accompanies every
deny case.
"""

import json

import pytest

from hooks.scripts.hook_router import handle_block_raw_review_post


def _bash_event(command: str, tool_name: str = "Bash") -> dict:
    return {
        "session_id": "sess-review-post",
        "tool_name": tool_name,
        "tool_input": {"command": command},
    }


def _parse_deny(capsys: pytest.CaptureFixture[str]) -> dict | None:
    output = capsys.readouterr().out.strip()
    return json.loads(output) if output else None


class TestDeniesRawReviewWrites:
    """Raw forge REST writes to a review-comment endpoint are denied."""

    @pytest.mark.parametrize(
        "command",
        [
            "glab api projects/42/merge_requests/7/discussions -X POST -f body='looks good'",
            "glab api projects/42/merge_requests/7/discussions --method POST -f body=x",
            "glab api projects/42/merge_requests/7/notes -X POST -f body='nit'",
            "glab api projects/42/issues/9/notes --method POST --field body=hi",
            "gh api repos/o/r/pulls/12/comments -f body='please fix'",
            "gh api repos/o/r/issues/12/comments --method POST -f body=x",
            "gh api repos/o/r/pulls/12/comments -X POST --raw-field body=@note.txt",
            # Body flag with NO explicit method — gh/glab default to POST (#1568).
            "glab api projects/42/merge_requests/7/discussions -f body=hi",
            # Explicit non-GET write methods stay writes even with a body flag.
            "glab api projects/42/merge_requests/7/comments --method PATCH -f body=x",
            "glab api projects/42/merge_requests/7/notes -X PUT -f body=x",
            # Repeated method flags resolve LAST-WINS in gh (2.87.3) / glab
            # (1.80.4): a GET token followed by a write method is a genuine
            # write, not a read — the bypass the cold-review flagged (#1568).
            "gh api repos/o/r/pulls/12/comments -X GET -X POST -f body=hi",
            "glab api projects/42/merge_requests/7/discussions --method=GET --method PATCH -f body=x",
            # DELETE is a write — an effective GET is the ONLY read.
            "glab api projects/42/merge_requests/7/notes -X DELETE -f x",
            # ISSUE/work-item note DELETE: the exact raw bypass the sanctioned
            # `t3 review delete-issue-note` replaces — still hard-denied here.
            "glab api projects/42/issues/8568/notes/3456141979 --method DELETE",
            "glab api projects/42/issues/8568/notes/3456141979 -X DELETE",
            # pflag NO-SPACE shorthand (`-XPOST`/`-XPUT`) is a real method
            # override; the spaced-only regex missed it, leaving the IDENTICAL
            # `-XPUT` bypass on this gate. Must be DENIED.
            "gh api repos/o/r/pulls/12/comments -XPOST -f body=hi",
            "glab api projects/42/merge_requests/7/notes -XPUT -f body=x",
            # No-space last-wins: earlier GET overridden by trailing POST → write.
            "gh api repos/o/r/pulls/12/comments -XGET -XPOST -f body=hi",
        ],
    )
    def test_raw_review_write_is_denied(self, command: str, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_block_raw_review_post(_bash_event(command)) is True
        deny = _parse_deny(capsys)
        assert deny is not None
        assert deny["permissionDecision"] == "deny"

    def test_deny_message_names_the_sanctioned_cli(self, capsys: pytest.CaptureFixture[str]) -> None:
        command = "glab api projects/42/merge_requests/7/discussions -X POST -f body='hi'"
        handle_block_raw_review_post(_bash_event(command))
        deny = _parse_deny(capsys)
        assert deny is not None
        reason = deny["permissionDecisionReason"]
        assert "review post-comment" in reason
        assert "post-draft-note" in reason
        assert "draft" in reason
        assert "dedup" in reason
        assert "on-behalf approval" in reason

    def test_deny_message_names_the_sanctioned_delete_clis(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A blocked issue-note DELETE points at the sanctioned delete CLIs, not just the post ones."""
        command = "glab api projects/42/issues/8568/notes/3456141979 --method DELETE"
        handle_block_raw_review_post(_bash_event(command))
        deny = _parse_deny(capsys)
        assert deny is not None
        reason = deny["permissionDecisionReason"]
        assert "delete-issue-note" in reason
        assert "delete-discussion" in reason


class TestAllowsReadsAndUnrelated:
    """Bare reads and non-review commands pass through with no false-deny."""

    @pytest.mark.parametrize(
        "command",
        [
            # GET read of a review endpoint — no write flags.
            "glab api projects/42/merge_requests/7/discussions",
            "glab api projects/42/merge_requests/7/notes --paginate",
            "gh api repos/o/r/pulls/12/comments",
            # Explicit GET read with a body flag carrying a query param (#1568):
            # `-X GET`/`--method GET` forces a GET, so `-f` is a query param,
            # never a body write — must NOT be denied.
            "glab api projects/42/merge_requests/7/discussions -X GET -f sort=asc",
            "glab api projects/42/merge_requests/7/discussions --method GET -f sort=asc",
            "gh api repos/o/r/pulls/12/notes --method=GET -f per_page=100",
            # No-space explicit GET forces a read — must NOT over-block.
            "glab api projects/42/merge_requests/7/discussions -XGET -f sort=asc",
            # Repeated method flags, GET LAST — effective method is GET, so a
            # write-then-GET command is a read (last-wins, no false-deny).
            "gh api repos/o/r/pulls/12/comments -X POST -X GET",
            # Non-review forge reads/writes.
            "glab api projects/42/merge_requests/7",
            "glab api projects/42/merge_requests/7/approvals -X POST",
            "gh api repos/o/r/pulls/12 -f title='x'",
            "gh api repos/o/r/labels -f name=bug",
            # Unrelated commands.
            "git status",
            "ls -la",
            "echo 'glab api discussions -X POST is just a string here'",
            "t3 teatree review post-comment 7 --file a.py --line 3 --body x",
            # The sanctioned issue-note delete CLI is NOT a raw forge api call —
            # it must pass through (it routes through the on-behalf gate itself).
            "t3 teatree review delete-issue-note org/repo 8568 3456141979",
            "t3 teatree review delete-discussion org/repo 7 99",
        ],
    )
    def test_command_is_allowed(self, command: str, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_block_raw_review_post(_bash_event(command)) is not True
        assert capsys.readouterr().out.strip() == ""

    def test_ignores_non_bash_tools(self, capsys: pytest.CaptureFixture[str]) -> None:
        command = "glab api projects/42/merge_requests/7/discussions -X POST -f body=x"
        assert handle_block_raw_review_post(_bash_event(command, tool_name="Read")) is not True
        assert capsys.readouterr().out.strip() == ""

    def test_empty_command_passes_through(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_block_raw_review_post(_bash_event("")) is not True
        assert capsys.readouterr().out.strip() == ""
