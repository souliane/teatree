import pytest

from teatree.agents.lane_b import mcp


class TestMcpToolsets:
    def test_degrades_to_empty_when_client_absent(self, monkeypatch) -> None:
        monkeypatch.setattr(mcp, "mcp_client_available", lambda: False)
        assert mcp.build_mcp_toolsets() == []

    def test_command_default_is_the_teatree_read_only_server(self) -> None:
        assert mcp.TEATREE_MCP_STDIO_COMMAND == ("t3", "mcp", "serve")

    @pytest.mark.skipif(not mcp.mcp_client_available(), reason="pydantic_ai MCP client (fastmcp) not installed")
    def test_builds_a_stdio_server_when_client_present(self, monkeypatch) -> None:
        import pydantic_ai.mcp as pai_mcp  # noqa: PLC0415 — module import fails without the fastmcp extra; guarded by skipif.

        captured: dict = {}

        class _FakeStdio:
            def __init__(self, command: str, *, args: list[str]) -> None:
                captured["command"] = command
                captured["args"] = args

        monkeypatch.setattr(pai_mcp, "MCPServerStdio", _FakeStdio)
        toolsets = mcp.build_mcp_toolsets(command=("t3", "mcp", "serve"))
        assert len(toolsets) == 1
        assert captured == {"command": "t3", "args": ["mcp", "serve"]}
