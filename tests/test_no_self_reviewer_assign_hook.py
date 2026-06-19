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
            # Out-of-band REST writes that set the reviewer list directly.
            ("glab api --method PUT projects/acme-eng%2Fwidget-app/merge_requests/7624 -f reviewer_ids=42"),
            ('gh api --method POST repos/souliane/teatree/pulls/12/requested_reviewers -f "reviewers[]=octocat"'),
        ],
    )
    def test_reviewer_assignment_is_blocked(self, command: str, capsys: pytest.CaptureFixture[str]) -> None:
        assert router.handle_block_self_reviewer_assign(_bash(command)) is True
        deny = _parse_deny(capsys)
        assert deny is not None
        assert deny["permissionDecision"] == "deny"
        assert "reviewer" in deny["permissionDecisionReason"].lower()

    def test_mcp_update_with_reviewer_is_blocked(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = {
            "session_id": "sess-reviewer",
            "tool_name": "mcp__glab__glab_mr_update",
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
            # A READ of the reviewer list is not an assignment.
            "glab api projects/acme-eng%2Fwidget-app/merge_requests/7624",
            # Requesting review via the approval channel is the sanctioned path.
            "glab mr view 7624",
            # The literal phrase embedded in a commit message is not an assignment.
            "git commit -m 'note: glab mr update --reviewer was the old buggy path'",
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
