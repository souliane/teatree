"""``t3 setup`` MCP-server-registration confirmation step (souliane/teatree#2863)."""

import json
from pathlib import Path

import pytest

from teatree.cli.setup.mcp_registrar import McpServerRegistrar


class TestMcpServerRegistrarVerify:
    def test_ok_when_mcp_json_declares_teatree(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        (tmp_path / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"teatree": {"command": "t3", "args": ["mcp", "serve"]}}}),
        )

        assert McpServerRegistrar(tmp_path).verify() is True
        out = capsys.readouterr().out
        assert "OK" in out
        assert "teatree" in out

    def test_warns_when_mcp_json_is_missing(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert McpServerRegistrar(tmp_path).verify() is False
        out = capsys.readouterr().out
        assert "WARN" in out

    def test_idempotent_across_repeated_runs(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        (tmp_path / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"teatree": {"command": "t3", "args": ["mcp", "serve"]}}}),
        )
        registrar = McpServerRegistrar(tmp_path)

        first = registrar.verify()
        capsys.readouterr()
        second = registrar.verify()
        out = capsys.readouterr().out

        assert first is True
        assert second is True
        assert "OK" in out
