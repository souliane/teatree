# test-path: cross-cutting — tests hook_router.py (hooks/), which has no single src/teatree/ mirror.
"""Session start names no MCP server but teatree's own.

An enabled third-party server earns no notice, and the account-switch directive that
still fires on a real `/login` names nothing to re-authenticate outside teatree.
"""

import json
import re
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from teatree.core.account_fingerprint import record_fingerprint

_FOREIGN_MCP_TEXT = re.compile(r"claude[.]ai|mcp[ ]reconnect|claude mcp|connector", re.IGNORECASE)
_ENABLED_THIRD_PARTY = {
    "mcpServers": {"notion": {"type": "http", "url": "https://mcp.notion.com/mcp"}},
    "claudeAiMcpEverConnected": ["claude.ai Google Drive"],
}


def _write_claude_json(home: Path, payload: dict) -> None:
    (home / ".claude.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def staged_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    return tmp_path


def test_an_enabled_third_party_server_adds_no_notice(staged_home: Path) -> None:
    _write_claude_json(staged_home, _ENABLED_THIRD_PARTY)

    merged = router._merge_session_start_context("BASE DIRECTIVE", "sess-1", "startup", router.StartClaims())

    assert merged == "BASE DIRECTIVE"


def test_a_pending_account_switch_leads_with_its_directive_and_names_no_foreign_mcp(staged_home: Path) -> None:
    _write_claude_json(staged_home, {**_ENABLED_THIRD_PARTY, "oauthAccount": {"accountUuid": "uuid-B"}})
    record_fingerprint("uuid-A", home=staged_home)

    merged = router._merge_session_start_context("BASE DIRECTIVE", "sess-1", "startup", router.StartClaims())

    assert merged.startswith("TEATREE — Claude account switch detected")
    assert "`t3 setup recover-account-switch`" in merged
    assert merged.endswith("BASE DIRECTIVE")
    assert _FOREIGN_MCP_TEXT.findall(merged) == []
