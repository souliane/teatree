"""Teatree's own MCP server registration verification (souliane/teatree#2863)."""

import json
from pathlib import Path

from teatree.core.mcp_registration import (
    TEATREE_MCP_SERVER_NAME,
    mcp_json_path,
    read_declared_mcp_servers,
    verify_teatree_mcp_registration,
)

_VALID_ENTRY = {"command": "t3", "args": ["mcp", "serve"]}


class TestMcpJsonPath:
    def test_joins_the_repo_root(self, tmp_path: Path) -> None:
        assert mcp_json_path(tmp_path) == tmp_path / ".mcp.json"


class TestReadDeclaredMcpServers:
    def test_missing_file_reads_as_empty(self, tmp_path: Path) -> None:
        assert read_declared_mcp_servers(tmp_path / ".mcp.json") == {}

    def test_malformed_json_reads_as_empty(self, tmp_path: Path) -> None:
        path = tmp_path / ".mcp.json"
        path.write_text("not json")
        assert read_declared_mcp_servers(path) == {}

    def test_non_dict_json_reads_as_empty(self, tmp_path: Path) -> None:
        path = tmp_path / ".mcp.json"
        path.write_text(json.dumps(["not", "a", "dict"]))
        assert read_declared_mcp_servers(path) == {}

    def test_reads_the_flat_shape(self, tmp_path: Path) -> None:
        path = tmp_path / ".mcp.json"
        path.write_text(json.dumps({"teatree": _VALID_ENTRY}))
        assert read_declared_mcp_servers(path) == {"teatree": _VALID_ENTRY}

    def test_reads_the_mcp_servers_wrapped_shape(self, tmp_path: Path) -> None:
        path = tmp_path / ".mcp.json"
        path.write_text(json.dumps({"mcpServers": {"teatree": _VALID_ENTRY}}))
        assert read_declared_mcp_servers(path) == {"teatree": _VALID_ENTRY}

    def test_skips_non_dict_entries(self, tmp_path: Path) -> None:
        path = tmp_path / ".mcp.json"
        path.write_text(json.dumps({"teatree": _VALID_ENTRY, "bogus": "not-a-dict"}))
        assert read_declared_mcp_servers(path) == {"teatree": _VALID_ENTRY}


class TestVerifyTeatreeMcpRegistration:
    def test_ok_when_entry_matches_expected_shape(self, tmp_path: Path) -> None:
        (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {TEATREE_MCP_SERVER_NAME: _VALID_ENTRY}}))

        outcome = verify_teatree_mcp_registration(tmp_path)

        assert outcome.ok is True
        assert TEATREE_MCP_SERVER_NAME in outcome.message

    def test_fails_when_mcp_json_is_missing(self, tmp_path: Path) -> None:
        outcome = verify_teatree_mcp_registration(tmp_path)

        assert outcome.ok is False
        assert "does not declare" in outcome.message

    def test_fails_when_teatree_entry_is_absent(self, tmp_path: Path) -> None:
        (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"other-server": _VALID_ENTRY}}))

        outcome = verify_teatree_mcp_registration(tmp_path)

        assert outcome.ok is False
        assert "does not declare" in outcome.message

    def test_fails_when_command_does_not_match(self, tmp_path: Path) -> None:
        entry = {"command": "python", "args": ["mcp", "serve"]}
        (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {TEATREE_MCP_SERVER_NAME: entry}}))

        outcome = verify_teatree_mcp_registration(tmp_path)

        assert outcome.ok is False
        assert "python" in outcome.message

    def test_fails_when_args_do_not_match(self, tmp_path: Path) -> None:
        entry = {"command": "t3", "args": ["mcp", "list"]}
        (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {TEATREE_MCP_SERVER_NAME: entry}}))

        outcome = verify_teatree_mcp_registration(tmp_path)

        assert outcome.ok is False
