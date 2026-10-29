"""Foreign MCP policy through the configured PreToolUse router."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests._gate_escape_audit import write_recording_t3

_ROOT = Path(__file__).resolve().parents[1]
_CONFIG = _ROOT / "hooks" / "hooks.json"
_ROUTER = _ROOT / "hooks" / "scripts" / "hook_router.py"


def _routed(tool_name: str) -> bool:
    entries = json.loads(_CONFIG.read_text())["hooks"]["PreToolUse"]
    return any(
        re.fullmatch(entry["matcher"], tool_name)
        and any("hook_router.py" in hook.get("command", "") for hook in entry["hooks"])
        for entry in entries
    )


def _invoke(tool_name: str, tool_input: dict[str, str], home: Path) -> subprocess.CompletedProcess[str]:
    payload = {"session_id": "foreign-policy", "cwd": str(home), "tool_name": tool_name, "tool_input": tool_input}
    return subprocess.run(
        [sys.executable, str(_ROUTER), "--event", "PreToolUse"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env={**os.environ, "HOME": str(home), "XDG_DATA_HOME": str(home / "data")},
    )


@pytest.mark.parametrize(
    "tool_name",
    [
        "mcp__plugin_notion__update_page",
        "mcp__plugin_notion__fetch_page",
        "mcp__other__read_resource",
        "mcp__other__write_resource",
        "mcp__glab__glab_mr_create",
        "mcp__slack__slack_search",
        "mcp__teatree_extra__ticket_get",
        "mcp__plugin_t3_teatreex__ticket_get",
        "mcp__teatree__evil__ticket_get",
    ],
)
def test_foreign_tool_is_routed_and_denied(tool_name: str, tmp_path: Path) -> None:
    assert _routed(tool_name)
    result = _invoke(tool_name, {"query": "sample"}, tmp_path)
    assert result.returncode == 2, result.stdout + result.stderr
    specific = json.loads(result.stdout)["hookSpecificOutput"]
    assert specific["gate_id"] == "foreign_mcp"
    reason = specific["permissionDecisionReason"]
    assert "teatree" in reason
    assert "t3" in reason


@pytest.mark.parametrize("tool_name", ["mcp__teatree__ticket_get", "mcp__plugin_t3_teatree__ticket_get", "Bash"])
def test_own_server_and_non_mcp_tool_pass(tool_name: str, tmp_path: Path) -> None:
    result = _invoke(tool_name, {"query": "sample"}, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_escape_is_audited_and_allows_one_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    record = write_recording_t3(tmp_path / "bin")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    result = _invoke("mcp__other__read_resource", {"query": "[gate-escape:foreign_mcp: vetted call]"}, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(record.read_text())["gate"] == "foreign_mcp"
    denied = _invoke("mcp__other__read_resource", {"query": "sample"}, tmp_path)
    assert denied.returncode == 2
