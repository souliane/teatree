import json

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.void_merge_guard import handle_block_void_merge

_COMMAND = "t3 teatree pr void-merge https://github.com/example-org/example-repo/pull/41 --reason x --by alice"


def _event(command: str, tool_name: str = "Bash") -> dict:
    return {"session_id": "sess-void-merge", "tool_name": tool_name, "tool_input": {"command": command}}


def test_an_agent_cannot_run_a_writing_void(capsys: pytest.CaptureFixture[str]) -> None:
    assert handle_block_void_merge(_event(_COMMAND)) is True

    deny = json.loads(capsys.readouterr().out)
    assert deny["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "only a human at a terminal" in deny["hookSpecificOutput"]["permissionDecisionReason"]


def test_a_dry_run_and_other_tools_pass(capsys: pytest.CaptureFixture[str]) -> None:
    assert handle_block_void_merge(_event(f"{_COMMAND} --dry-run")) is False
    assert handle_block_void_merge(_event(_COMMAND, tool_name="Edit")) is False
    assert capsys.readouterr().out == ""


def test_the_router_runs_it_on_every_pre_tool_use() -> None:
    assert handle_block_void_merge in router._HANDLERS["PreToolUse"]
