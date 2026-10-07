import sys
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from teatree.agents.lane_b import mcp


@dataclass(frozen=True)
class _FakeStdio:
    command: str
    args: list[str]


@pytest.mark.parametrize("serve_flags", [["--read-only", "--allow-write", "review_request_post"], ["--read-only"], []])
def test_the_launched_server_carries_the_phase_flags(monkeypatch: pytest.MonkeyPatch, serve_flags: list[str]) -> None:
    monkeypatch.setattr(mcp, "mcp_client_available", lambda: True)
    monkeypatch.setitem(sys.modules, "pydantic_ai.mcp", SimpleNamespace(MCPServerStdio=_FakeStdio))

    [server] = mcp.build_mcp_toolsets(serve_flags=serve_flags)

    assert (server.command, server.args) == ("t3", ["mcp", "serve", *serve_flags])


class TestMcpToolsets:
    def test_degrades_to_empty_when_client_absent(self, monkeypatch) -> None:
        monkeypatch.setattr(mcp, "mcp_client_available", lambda: False)
        assert mcp.build_mcp_toolsets(serve_flags=[]) == []

    def test_present_but_unusable_client_degrades_instead_of_crashing(self, monkeypatch) -> None:
        # `pydantic-ai-slim[mcp]` resolves `fastmcp-slim`, whose client imports
        # `mcp.McpError` — renamed `MCPError` in the mcp 2.x this project pins. The
        # module is then IMPORTABLE BY NAME while `pydantic_ai.mcp` raises, so a
        # presence probe answers yes and the unguarded import crashes every phased
        # dispatch. Availability must mean "the toolset imports", not "a module of
        # that name exists".
        monkeypatch.setattr(mcp, "find_spec", lambda _name: object())
        assert mcp.mcp_client_available() is False
        assert mcp.build_mcp_toolsets(serve_flags=[]) == []

    def test_command_default_is_the_teatree_server(self) -> None:
        assert mcp.TEATREE_MCP_STDIO_COMMAND == ("t3", "mcp", "serve")
