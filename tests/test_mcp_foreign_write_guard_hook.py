# test-path: cross-cutting — binds the hook guard's teatree-server exemption to the server name core registers.
"""Foreign MCP writes are refused through the live PreToolUse router."""

import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import handle_block_foreign_mcp_write
from hooks.scripts.mcp_foreign_write_guard import (
    _NOTION_DENY_REASON,
    TEATREE_MCP_SERVERS,
    is_foreign_mcp_write,
    is_slack_mcp_write,
)
from teatree.cli.notion import notion_app
from teatree.core.mcp_registration import TEATREE_MCP_SERVER_NAME

_ROOT = Path(__file__).resolve().parents[1]


def _run_pretool(tool_name: str, tmp_path: Path, tool_input: dict | None = None) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "T3_CONFIG_DB": str(tmp_path / "absent-config.sqlite3"),
        "TEATREE_CLAUDE_STATUSLINE_STATE_DIR": str(tmp_path / "state"),
        "T3_HOOK_STATE_DIR": str(tmp_path / "state"),
    }
    event = _event(tool_name, tool_input)
    event["session_id"] = f"sess-{tmp_path.name}"
    return subprocess.run(
        [sys.executable, str(_ROOT / "hooks/scripts/hook_router.py"), "--event", "PreToolUse"],
        input=json.dumps(event),
        text=True,
        capture_output=True,
        cwd=_ROOT,
        env=env,
        check=False,
        timeout=15,
    )


@pytest.mark.parametrize(
    "tool_name",
    [
        "mcp__notion__notion-update-page",
        "mcp__notion__notion-create-comment",
        "mcp__notion__notion-append-block",
        "mcp__slack__slack_send_message",
        "mcp__other__create_ticket",
        "mcp__other__get_then_delete",
        "mcp__teatree_x__create_ticket",
        "mcp__plugin_t3_teatreex__create_ticket",
        "mcp__teatree__evil__create_ticket",
    ],
)
def test_foreign_mcp_write_denied_by_router(tool_name: str, tmp_path: Path) -> None:
    result = _run_pretool(tool_name, tmp_path)
    assert result.returncode == 2, result.stderr
    denial = json.loads(result.stdout)
    assert denial["permissionDecision"] == "deny"
    assert "t3" in denial["permissionDecisionReason"]


@pytest.mark.parametrize(
    "tool_name",
    [
        "mcp__notion__notion-fetch",
        "mcp__notion__notion-search",
        "mcp__notion__API-retrieve-a-page",
        "mcp__slack__slack_get_channel_history",
        "mcp__teatree__slack_react",
        "mcp__plugin_t3_teatree__notion_comment",
    ],
)
def test_read_and_teatree_mcp_tool_pass_router(tool_name: str, tmp_path: Path) -> None:
    result = _run_pretool(tool_name, tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_foreign_mcp_matcher_routes_only_foreign_servers() -> None:
    hooks_json = (_ROOT / "hooks/hooks.json").read_text(encoding="utf-8")
    assert "notion" not in hooks_json.lower()
    entries = json.loads(hooks_json)["hooks"]["PreToolUse"]
    matchers = [entry["matcher"] for entry in entries if entry["matcher"].startswith("mcp__")]
    foreign = [matcher for matcher in matchers if "teatree" in matcher]
    assert len(foreign) == 1
    assert "|" not in foreign[0]
    assert set(re.findall(r"\(\?!(\w+)__\(\?!\.\*__\)\)", foreign[0])) == TEATREE_MCP_SERVERS
    for tool_name in ("mcp__notion__notion-update-page", "mcp__other__create_ticket", "mcp__glab__glab_mr_create"):
        assert re.fullmatch(foreign[0], tool_name), tool_name
    for tool_name in ("mcp__teatree__ticket_get", "mcp__plugin_t3_teatree__ticket_get"):
        assert not any(re.fullmatch(matcher, tool_name) for matcher in matchers), tool_name
    for tool_name in (
        "mcp__teatree_x__create_ticket",
        "mcp__plugin_t3_teatreex__create_ticket",
        "mcp__teatree__evil__create_ticket",
    ):
        assert re.fullmatch(foreign[0], tool_name), tool_name


def test_notion_redirect_verbs_are_registered_commands() -> None:
    named = re.findall(r"`t3 notion ([^`]+)`", _NOTION_DENY_REASON)
    assert set(named) >= {
        "fetch",
        "create",
        "replace",
        "append",
        "comments",
        "comment post",
        "query",
        "section replace",
    }
    for route in named:
        parts = route.split()
        app = notion_app
        for group_name in parts[:-1]:
            app = next(group.typer_instance for group in app.registered_groups if group.name == group_name)
        assert parts[-1] in {command.name for command in app.registered_commands}, route


def test_existing_slack_switch_only_allows_slack_write(tmp_path: Path) -> None:
    db = tmp_path / "absent-config.sqlite3"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE teatree_config_setting "
            "(id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', ?, 'false')",
            ("mcp_slack_write_gate_enabled",),
        )
        conn.commit()
    finally:
        conn.close()
    result = _run_pretool("mcp__slack__slack_send_message", tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    foreign = _run_pretool("mcp__other__create_ticket", tmp_path)
    assert foreign.returncode == 2, foreign.stderr


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
            "mcp__Slack__slack_add_reaction",
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
        assert handle_block_foreign_mcp_write(_event(tool_name)) is True
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
            "mcp__Slack__slack_create_conversation",
            "mcp__Slack__slack_create_canvas",
            "mcp__slack__conversations_invite",
            "mcp__slack__slack_pin_message",
            "mcp__slack__admin_conversations_archive",
        ],
    )
    def test_unrecognised_slack_tool_is_denied(self, tool_name: str, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_block_foreign_mcp_write(_event(tool_name)) is True
        assert _parse_deny(capsys) is not None


class TestAllowsReadsAndOwnTools:
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
            "mcp__Slack__slack_get_reactions",
            "mcp__notion__notion-query-database",
            "mcp__notion__API-retrieve-a-page",
            "Bash",
        ],
    )
    def test_read_or_own_tool_passes(self, tool_name: str, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_block_foreign_mcp_write(_event(tool_name)) is False
        assert _parse_deny(capsys) is None


class TestTeatreesOwnServerIsNotADirectSlackWrite:
    """teatree's own MCP Slack tools go through its backends and egress gates, so the guard lets them by."""

    @pytest.mark.parametrize(
        "tool_name",
        [
            "mcp__teatree__slack_react",
            "mcp__plugin_t3_teatree__slack_react",
            "mcp__plugin_t3_teatree__slack_mentions",
        ],
    )
    def test_a_teatree_server_tool_passes(self, tool_name: str, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_block_foreign_mcp_write(_event(tool_name)) is False
        assert _parse_deny(capsys) is None

    @pytest.mark.parametrize(
        "tool_name",
        [
            "mcp__teatree_x__slack_react",
            "mcp__plugin_t3_teatreex__slack_react",
            "mcp__xteatree__slack_send_message",
            "mcp__slack__teatree_send_message",
            "mcp__teatree__evil__slack_send_message",
            "mcp__plugin_t3_teatree__x__slack_react",
            "mcp__Teatree__slack_react",
            "mcp__plugin_t3_TEATREE__slack_mentions",
        ],
    )
    def test_a_lookalike_server_is_still_denied(self, tool_name: str, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_block_foreign_mcp_write(_event(tool_name)) is True
        assert _parse_deny(capsys) is not None

    def test_the_exempt_servers_are_the_names_teatree_registers(self) -> None:
        plugin = json.loads((_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["name"]
        declared = json.loads((_ROOT / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]

        assert set(declared) == {TEATREE_MCP_SERVER_NAME}
        assert {TEATREE_MCP_SERVER_NAME, f"plugin_{plugin}_{TEATREE_MCP_SERVER_NAME}"} == TEATREE_MCP_SERVERS


class TestClassifier:
    def test_slack_write_verbs_classified(self) -> None:
        assert is_slack_mcp_write("mcp__slack__slack_send_message") is True
        assert is_slack_mcp_write("mcp__slack__slack_get_channel_history") is False
        assert is_slack_mcp_write("mcp__glab__glab_mr_create") is False

    def test_unknown_slack_tool_fails_closed(self) -> None:
        assert is_slack_mcp_write("mcp__slack__slack_frobnicate_workspace") is True

    def test_foreign_write_classifies_by_read_verbs_and_exact_server(self) -> None:
        assert is_foreign_mcp_write("mcp__other__create_ticket") is True
        assert is_foreign_mcp_write("mcp__other__unknown_action") is True
        assert is_foreign_mcp_write("mcp__notion__API-retrieve-a-page") is False
        assert is_foreign_mcp_write("mcp__slack__slack_query_workspace") is True
        assert is_foreign_mcp_write("mcp__teatree__create_ticket") is False


class TestNeverLockout:
    def test_escape_token_allows_a_single_write(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tool_input = {"channel": "C-x", "text": "hi [gate-escape:mcp_foreign_write: vetted one-off]"}
        event = _event("mcp__slack__slack_send_message", tool_input)
        event["cwd"] = str(_ROOT)
        current = {"event": event}
        monkeypatch.setattr(router, "_current_hook_context", lambda: ("PreToolUse", current["event"]))
        requests = []

        def audit_stub(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            requests.append(json.loads(str(kwargs["stdin_text"])))
            return subprocess.CompletedProcess(argv, 0, '{"allowed": true}', "")

        monkeypatch.setattr(router._gate_escape, "run_t3", audit_stub)
        assert handle_block_foreign_mcp_write(event) is False
        assert _parse_deny(capsys) is None
        assert len(requests) == 1
        assert requests[0]["gate"] == "mcp_foreign_write"
        current["event"] = _event("mcp__slack__slack_send_message")
        assert handle_block_foreign_mcp_write(current["event"]) is True
        assert _parse_deny(capsys) is not None

    def test_kill_switch_disables_the_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def setting(name: str, *, default: bool = True) -> bool:
            assert name == "mcp_slack_write_gate_enabled"
            return False

        monkeypatch.setattr(router, "_teatree_bool_setting", setting)
        assert handle_block_foreign_mcp_write(_event("mcp__slack__slack_send_message")) is False
