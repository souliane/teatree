"""Tests for the direct-MCP Slack-write deny gate (#1196).

A direct ``mcp__*slack*`` write bypasses teatree's Slack egress chokepoint
(on-behalf gate, voice classifier, verify-by-re-read), so this ``PreToolUse``
gate refuses every Slack MCP WRITE and redirects to the ``t3`` CLI. Slack MCP
READS pass through, and the gate is conservative — a no-false-deny guard
accompanies every deny case.
"""

import json

import pytest

from hooks.scripts.hook_router import handle_block_mcp_slack_write
from hooks.scripts.mcp_slack_write_guard import is_slack_mcp_write


def _event(tool_name: str, tool_input: dict | None = None) -> dict:
    return {
        "session_id": "sess-slack-mcp",
        "tool_name": tool_name,
        "tool_input": tool_input if tool_input is not None else {"channel": "C-eng", "text": "hi"},
    }


def _parse_deny(capsys: pytest.CaptureFixture[str]) -> dict | None:
    output = capsys.readouterr().out.strip()
    return json.loads(output) if output else None


class TestDeniesSlackMcpWrites:
    @pytest.mark.parametrize(
        "tool_name",
        [
            "mcp__slack__slack_send_message",
            "mcp__claude_ai_Slack__slack_add_reaction",
            "mcp__slack__chat_postMessage",
            "mcp__slack__reactions_add",
            "mcp__slack__chat_update",
            "mcp__slack__slack_reply_to_thread",
            "mcp__slack__slack_schedule_message",
            "mcp__slack__files_upload",
            "mcp__slack__chat_delete",
        ],
    )
    def test_write_tool_is_denied(self, tool_name: str, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_block_mcp_slack_write(_event(tool_name)) is True
        deny = _parse_deny(capsys)
        assert deny is not None
        assert "t3" in json.dumps(deny)


class TestDeniesUnrecognisedSlackTools:
    """An unrecognised Slack MCP tool is a WRITE until its READ shape is recognised.

    Every one of these carries no write verb, so the old verb-substring
    classifier passed them through — a channel created and a canvas published
    under the user's OAuth identity, outside the egress chokepoint.
    """

    @pytest.mark.parametrize(
        "tool_name",
        [
            "mcp__claude_ai_Slack__slack_create_conversation",
            "mcp__claude_ai_Slack__slack_create_canvas",
            "mcp__slack__conversations_invite",
            "mcp__slack__slack_pin_message",
            "mcp__slack__admin_conversations_archive",
        ],
    )
    def test_unrecognised_slack_tool_is_denied(self, tool_name: str, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_block_mcp_slack_write(_event(tool_name)) is True
        assert _parse_deny(capsys) is not None


class TestAllowsReadsAndNonSlack:
    @pytest.mark.parametrize(
        "tool_name",
        [
            "mcp__slack__slack_get_channel_history",
            "mcp__slack__conversations_list",
            "mcp__slack__search_messages",
            "mcp__slack__slack_get_users",
            "mcp__slack__conversationsHistory",
            "mcp__slack__users_info",
            # A pure READ whose noun happens to carry a write verb — the
            # substring classifier blocked it and pointed at no CLI equivalent.
            "mcp__claude_ai_Slack__slack_get_reactions",
            "mcp__glab__glab_mr_create",
            "mcp__notion__create_page",
            "Bash",
        ],
    )
    def test_read_or_non_slack_tool_passes(self, tool_name: str, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_block_mcp_slack_write(_event(tool_name)) is False
        assert _parse_deny(capsys) is None


class TestClassifier:
    def test_slack_write_verbs_classified(self) -> None:
        assert is_slack_mcp_write("mcp__slack__slack_send_message") is True
        assert is_slack_mcp_write("mcp__slack__slack_get_channel_history") is False
        assert is_slack_mcp_write("mcp__glab__glab_mr_create") is False

    def test_unknown_slack_tool_fails_closed(self) -> None:
        assert is_slack_mcp_write("mcp__slack__slack_frobnicate_workspace") is True


class TestNeverLockout:
    def test_escape_token_allows_a_single_write(self, capsys: pytest.CaptureFixture[str]) -> None:
        tool_input = {"channel": "C-x", "text": "hi [slack-mcp-ok: vetted one-off]"}
        event = _event("mcp__slack__slack_send_message", tool_input)
        assert handle_block_mcp_slack_write(event) is False
        assert _parse_deny(capsys) is None

    def test_kill_switch_disables_the_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import hooks.scripts.hook_router as router  # noqa: PLC0415

        monkeypatch.setattr(router, "_teatree_bool_setting", lambda name, default=True: False)
        assert handle_block_mcp_slack_write(_event("mcp__slack__slack_send_message")) is False
