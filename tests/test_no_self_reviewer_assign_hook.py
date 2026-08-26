# test-path: cross-cutting — tests hook_router.py / no_self_reviewer_assign.py (hooks/), no src/teatree/ mirror.
"""Tests for the never-directly-assign-reviewers PreToolUse gate.

Reviewers must NEVER be directly assigned on a GitLab MR — least of all the
user's own MR (this happened on the user's MRs and is forbidden). Review is
*requested* via the Slack/approval channel only; teatree has no legitimate
direct-assignment path. The gate BLOCKS every reviewer-assignment surface:

* ``glab mr update <iid> --reviewer <user>`` (the CLI path that drove the bug);
* the out-of-band ``glab api``/``gh api`` write that sets ``reviewer_ids`` /
    ``reviewers`` / ``requested_reviewers`` on a merge_requests/pulls endpoint;
* the ``mcp__glab__glab_mr_update`` MCP tool carrying a ``reviewer`` arg.

It is never-lockout: a per-call ``[reviewer-ok: <reason>]`` token and the
``no_self_reviewer_assign_gate_enabled`` kill-switch both ALLOW; the deny
routes through ``_fail_open_or_deny`` (self-rescue + master switch + breaker).

The gate is SEPARATE from the MR-metadata gate: the metadata gate SKIPS a
``--reviewer`` update (validates only title/description, never-lockout), so it
never saw the bug. This gate is the missing block.
"""

import json

import pytest

import hooks.scripts.hook_router as router
import hooks.scripts.no_self_reviewer_assign as reviewer_gate


def _bash(command: str) -> dict:
    return {"session_id": "sess-reviewer", "tool_name": "Bash", "tool_input": {"command": command}}


def _parse_deny(capsys: pytest.CaptureFixture[str]) -> dict | None:
    output = capsys.readouterr().out.strip()
    return json.loads(output) if output else None


class TestBlocksReviewerAssignment:
    @pytest.mark.parametrize(
        "command",
        [
            # The exact CLI surface that assigned a reviewer on the user's own MR.
            "glab mr update 7624 --reviewer WouterLachat",
            "glab mr update 7624 --reviewers WouterLachat,souliane",
            "glab mr update --reviewer WouterLachat -R acme-eng/widget-app",
            # glab mr CREATE that assigns a reviewer at creation time.
            "glab mr create --title 'fix: x (proj#1)' --description 'b' --reviewer WouterLachat",
            "glab mr create --reviewers WouterLachat,souliane",
            # gh pr CREATE assigning a reviewer — long flag and short -r.
            "gh pr create --title 'fix: x' --body 'b' --reviewer octocat",
            "gh pr create -r octocat -r hubot",
            # gh pr EDIT assigning a reviewer — --add-reviewer and --reviewer.
            "gh pr edit 12 --add-reviewer octocat",
            "gh pr edit 12 --reviewer octocat",
            # Out-of-band REST WRITES that set the reviewer list directly.
            ("glab api --method PUT projects/acme-eng%2Fwidget-app/merge_requests/7624 -f reviewer_ids=42"),
            ('gh api --method POST repos/souliane/teatree/pulls/12/requested_reviewers -f "reviewers[]=octocat"'),
            # A gh api POST inferred from a body field flag (no explicit --method).
            'gh api repos/souliane/teatree/pulls/12/requested_reviewers -f "reviewers[]=octocat"',
        ],
    )
    def test_reviewer_assignment_is_blocked(self, command: str, capsys: pytest.CaptureFixture[str]) -> None:
        assert router.handle_block_self_reviewer_assign(_bash(command)) is True
        deny = _parse_deny(capsys)
        assert deny is not None
        assert deny["permissionDecision"] == "deny"
        assert "reviewer" in deny["permissionDecisionReason"].lower()

    @pytest.mark.parametrize(
        "tool_name",
        ["mcp__glab__glab_mr_update", "mcp__glab__glab_mr_create"],
    )
    def test_mcp_with_reviewer_is_blocked(self, tool_name: str, capsys: pytest.CaptureFixture[str]) -> None:
        event = {
            "session_id": "sess-reviewer",
            "tool_name": tool_name,
            "tool_input": {"iid": 7624, "reviewer": "WouterLachat"},
        }
        assert router.handle_block_self_reviewer_assign(event) is True
        deny = _parse_deny(capsys)
        assert deny is not None
        assert deny["permissionDecision"] == "deny"


class TestAllowsNonReviewerSurfaces:
    @pytest.mark.parametrize(
        "command",
        [
            # A metadata-only update touching neither reviewer field — allowed here
            # (the metadata gate handles title/description separately).
            "glab mr update 12 --add-label needs-review",
            "glab mr update --title 'fix: rename widget (proj#1)'",
            "glab mr create --title 'fix: x (proj#1)' --description 'body'",
            # A GET READ of the requested-reviewers list is not an assignment —
            # default method, the reviewer field is on the path, no write flag.
            "gh api repos/souliane/teatree/pulls/12/requested_reviewers",
            "gh api --method GET repos/souliane/teatree/pulls/12/requested_reviewers",
            "glab api projects/acme-eng%2Fwidget-app/merge_requests/7624/reviewers",
            # A bare READ of the MR (no reviewer field at all).
            "glab api projects/acme-eng%2Fwidget-app/merge_requests/7624",
            # gh pr create/edit WITHOUT a reviewer flag is fine.
            "gh pr create --title 'fix: x' --body 'b'",
            "gh pr edit 12 --add-label needs-review",
            # -R (capital, the repo flag) must NOT be read as the -r reviewer
            # short-flag — guards a future regex broadening from re-colliding.
            "gh pr create --title 'fix: x' -R souliane/teatree --body 'b'",
            # Requesting review via the approval channel is the sanctioned path.
            "glab mr view 7624",
            # The literal phrase embedded in a commit message is not an assignment.
            "git commit -m 'note: glab mr update --reviewer was the old buggy path'",
            "git commit -m 'note: gh pr create --reviewer was the old buggy path'",
        ],
    )
    def test_non_reviewer_command_is_allowed(self, command: str, capsys: pytest.CaptureFixture[str]) -> None:
        assert router.handle_block_self_reviewer_assign(_bash(command)) is False
        assert _parse_deny(capsys) is None


class TestNeverLockout:
    def test_per_call_token_allows(self, capsys: pytest.CaptureFixture[str]) -> None:
        command = "glab mr update 7624 --reviewer WouterLachat  # [reviewer-ok: colleague MR, vetted]"
        assert router.handle_block_self_reviewer_assign(_bash(command)) is False
        assert _parse_deny(capsys) is None

    def test_empty_token_does_not_allow(self, capsys: pytest.CaptureFixture[str]) -> None:
        command = "glab mr update 7624 --reviewer WouterLachat  # [reviewer-ok: ]"
        assert router.handle_block_self_reviewer_assign(_bash(command)) is True
        deny = _parse_deny(capsys)
        assert deny is not None
        assert deny["permissionDecision"] == "deny"

    def test_kill_switch_disables_gate(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(reviewer_gate, "_gate_enabled", lambda: False)
        assert router.handle_block_self_reviewer_assign(_bash("glab mr update 7624 --reviewer X")) is False
        assert _parse_deny(capsys) is None


class TestQuotedFieldsAndLastWinsMethod:
    """The guard reads argument VALUES off the raw command, and the LAST method flag.

    Both failures let a real out-of-band reviewer write through: the routine
    quoted spelling ``-f 'reviewer_ids=42'`` was erased with the rest of the
    quoted spans before the field was looked for, and a leading ``-X GET``
    masked a later real write method that ``gh``/``glab`` resolve last-wins.
    """

    @pytest.mark.parametrize(
        "command",
        [
            "glab api --method PUT projects/acme-eng%2Fwidget-app/merge_requests/7624 -f 'reviewer_ids=42'",
            "gh api repos/souliane/teatree/pulls/12 -X GET -X PATCH -f 'requested_reviewers=octocat'",
        ],
    )
    def test_out_of_band_reviewer_write_is_blocked(self, command: str, capsys: pytest.CaptureFixture[str]) -> None:
        assert router.handle_block_self_reviewer_assign(_bash(command)) is True
        deny = _parse_deny(capsys)
        assert deny is not None
        assert deny["permissionDecision"] == "deny"

    @pytest.mark.parametrize(
        "command",
        [
            # A trailing GET is the effective method — a read, never an assignment.
            "gh api repos/souliane/teatree/pulls/12/requested_reviewers -X POST -X GET",
            # Prose mentioning reviewers is not a field assignment.
            "gh api repos/souliane/teatree/issues/1/comments -f body='the reviewers are deciding'",
        ],
    )
    def test_reads_and_prose_are_allowed(self, command: str, capsys: pytest.CaptureFixture[str]) -> None:
        assert router.handle_block_self_reviewer_assign(_bash(command)) is False
        assert _parse_deny(capsys) is None


class TestRawCurlIsNotABypass:
    """``curl`` reaches the same REST field the ``gh``/``glab api`` branch guards.

    Requiring a forge-CLI leader left the assignment one ``curl -X PUT`` away, so
    the gate certified a policy anyone could step around in one command.
    """

    @pytest.mark.parametrize(
        "command",
        [
            "curl -X PUT https://gitlab.com/api/v4/projects/1/merge_requests/76 -d '{\"reviewer_ids\": [42]}'",
            'curl --request PUT "$MR_URL" --data \'{"reviewer_ids":[42]}\'',
            'curl -XPOST https://api.github.com/repos/o/x/pulls/7/requested_reviewers -d \'{"reviewers":["bob"]}\'',
            # No method flag: curl's own body flag implies a POST.
            'curl https://api.github.com/repos/o/x/pulls/7/requested_reviewers --data-raw \'{"reviewers":["bob"]}\'',
        ],
    )
    def test_a_curl_reviewer_write_is_blocked(self, command: str, capsys: pytest.CaptureFixture[str]) -> None:
        assert router.handle_block_self_reviewer_assign(_bash(command)) is True
        deny = _parse_deny(capsys)
        assert deny is not None
        assert deny["permissionDecision"] == "deny"

    @pytest.mark.parametrize(
        "command",
        [
            # Listing the reviewers stays permitted — the read never-lockout.
            "curl https://api.github.com/repos/o/x/pulls/7/requested_reviewers",
            "curl -sSL -X GET https://gitlab.com/api/v4/projects/1/merge_requests/76",
            # ``-f`` is curl's --fail, NOT a body field: still a GET.
            "curl -fsSL https://api.github.com/repos/o/x/pulls/7/requested_reviewers",
            # A download whose path merely contains the word is not a field write.
            "curl -O https://example.com/reviewers.json",
        ],
    )
    def test_a_curl_read_is_allowed(self, command: str, capsys: pytest.CaptureFixture[str]) -> None:
        assert router.handle_block_self_reviewer_assign(_bash(command)) is False
        assert _parse_deny(capsys) is None

    def test_the_per_call_token_still_waives_a_curl_write(self, capsys: pytest.CaptureFixture[str]) -> None:
        command = (
            "curl -X PUT https://gitlab.com/api/v4/projects/1/merge_requests/76 "
            "-d '{\"reviewer_ids\": [42]}'  # [reviewer-ok: vetted colleague MR]"
        )
        assert router.handle_block_self_reviewer_assign(_bash(command)) is False
        assert _parse_deny(capsys) is None


class TestTheSanctionedPolicyCommandIsNotAnAssignmentSurface:
    """``review apply-reviewer-policy`` passes the PreToolUse guard.

    Not by a carve-out: the guard fires only on a forge CLI, a REST leader, or the
    MR-create/update MCP tools, and a ``t3`` command is none of those. Pinned here
    so the one sanctioned path cannot regress into a block.
    """

    @pytest.mark.parametrize(
        "command",
        [
            "t3 acme review apply-reviewer-policy",
            "t3 acme review apply-reviewer-policy --dry-run",
            "t3 acme review apply-reviewer-policy --json",
        ],
    )
    def test_the_policy_command_is_allowed(self, command: str, capsys: pytest.CaptureFixture[str]) -> None:
        assert router.handle_block_self_reviewer_assign(_bash(command)) is False
        assert _parse_deny(capsys) is None


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
    def test_every_general_reviewer_write_is_still_denied(
        self,
        command: str,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        assert router.handle_block_self_reviewer_assign(_bash(command)) is True
        deny = _parse_deny(capsys)
        assert deny is not None
        assert deny["permissionDecision"] == "deny"

    @pytest.mark.parametrize(
        "tool_name",
        ["mcp__glab__glab_mr_update", "mcp__glab__glab_mr_create"],
    )
    def test_the_mcp_reviewer_args_are_still_denied(
        self,
        tool_name: str,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        call = {"session_id": "s", "tool_name": tool_name, "tool_input": {"iid": "76", "reviewer": "octocat"}}

        assert router.handle_block_self_reviewer_assign(call) is True
        assert _parse_deny(capsys) is not None

    def test_the_deny_reason_names_the_sanctioned_command(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A blocked agent must be told the one path that exists, not that none does."""
        router.handle_block_self_reviewer_assign(_bash("glab mr update 76 --reviewer octocat"))

        deny = _parse_deny(capsys)
        assert deny is not None
        assert "apply-reviewer-policy" in deny["permissionDecisionReason"]
