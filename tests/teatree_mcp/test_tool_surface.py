"""The MCP registration boundary publishes and enforces approval tiers."""

import asyncio
from unittest.mock import patch

from mcp.types import ToolAnnotations

from teatree.backends.types import Service
from teatree.core.mcp_tool_group import ApprovalTier, McpTool, McpToolGroup
from teatree.core.overlay import OverlayConfig, OverlayConnectors
from teatree.mcp.server import build_server


def _write(subject: str) -> str:
    return subject


class _Contributing(OverlayConnectors):
    def __init__(self, tool: McpTool) -> None:
        self.tool = tool

    def mcp_tool_group(self) -> McpToolGroup:
        return McpToolGroup((self.tool,), "- demo_write(subject): test write.")


class _Overlay:
    def __init__(self, tool: McpTool | None = None) -> None:
        self.config = OverlayConfig(required_third_party_services=frozenset(Service))
        self.connectors = _Contributing(tool) if tool else OverlayConnectors()


def _tools(tool: McpTool | None = None) -> dict[str, object]:
    with patch("teatree.mcp.server.get_all_overlays", return_value={"demo": _Overlay(tool)}):
        return {entry.name: entry for entry in asyncio.run(build_server().list_tools())}


def test_existing_tools_publish_their_approval_tier() -> None:
    tools = _tools()

    assert tools["ticket_get"].meta["teatree/approval_tier"] == "read"
    assert tools["pr_create"].meta["teatree/approval_tier"] == "agent"
    assert tools["review_post_comment"].meta["teatree/approval_tier"] == "posture"
    assert tools["pr_merge"].meta["teatree/approval_tier"] == "exact"
    assert tools["notion_archive"].meta["teatree/approval_tier"] == "exact"
    assert tools["pr_merge"].description.startswith("[approval: exact]")


def test_overlay_write_with_seam_but_no_tier_is_withheld() -> None:
    tool = McpTool("demo_write", _write, ToolAnnotations(read_only_hint=False), seam="gated demo seam")

    assert "demo_write" not in _tools(tool)


def test_overlay_write_with_agent_tier_is_admitted() -> None:
    tool = McpTool(
        "demo_write",
        _write,
        ToolAnnotations(read_only_hint=False),
        seam="gated demo seam",
        tier=ApprovalTier.AGENT,
    )

    assert _tools(tool)["demo_write"].meta["teatree/approval_tier"] == "agent"


def test_destructive_posture_write_is_refused() -> None:
    tool = McpTool(
        "demo_write",
        _write,
        ToolAnnotations(read_only_hint=False, destructive_hint=True),
        seam="gated demo seam",
        tier=ApprovalTier.POSTURE,
    )

    assert "demo_write" not in _tools(tool)
