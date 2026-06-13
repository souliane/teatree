"""SessionStart enabled-MCP connectivity advisory (#2282).

The advisory rides the single SessionStart stdout write via
``_merge_session_start_context``; it fires when any MCP server is enabled
(a cheap, network-free ``~/.claude.json`` read — NOT the live probe, which
would exceed the 3s hook budget) and stays silent otherwise.
"""

import json
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router


def _write_claude_json(home: Path, payload: dict) -> None:
    (home / ".claude.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def staged_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    return tmp_path


class TestMcpConnectivityAdvisory:
    def test_no_servers_no_advisory(self, staged_home: Path) -> None:
        _write_claude_json(staged_home, {})
        assert router._mcp_connectivity_advisory() is None

    def test_missing_config_no_advisory(self, staged_home: Path) -> None:
        assert router._mcp_connectivity_advisory() is None

    def test_enabled_server_emits_advisory(self, staged_home: Path) -> None:
        _write_claude_json(staged_home, {"claudeAiMcpEverConnected": ["claude.ai Notion"]})
        advisory = router._mcp_connectivity_advisory()
        assert advisory is not None
        assert "MCP" in advisory
        assert "t3 doctor check" in advisory

    def test_merge_prepends_advisory_to_session_context(self, staged_home: Path) -> None:
        _write_claude_json(staged_home, {"claudeAiMcpEverConnected": ["claude.ai Notion"]})
        merged = router._merge_session_start_context("BASE DIRECTIVE", "sess-1", "startup")
        assert "BASE DIRECTIVE" in merged
        assert merged.index("t3 doctor check") < merged.index("BASE DIRECTIVE")

    def test_merge_no_servers_leaves_context_unchanged(self, staged_home: Path) -> None:
        _write_claude_json(staged_home, {})
        merged = router._merge_session_start_context("BASE DIRECTIVE", "sess-1", "startup")
        assert merged == "BASE DIRECTIVE"
