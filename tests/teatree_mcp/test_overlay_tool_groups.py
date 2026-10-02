"""An overlay contributing its own tool group to the MCP server.

The group is declarative — the overlay hands over its tools and the server does
the registering — because the server has to judge each one before it reaches an
agent. Two judgements, both fail-closed: a service no overlay declared, and a
write tool naming no gated seam. The second is the load-bearing one: the seam
allowlist is what keeps an ungated mutation off this surface, and an overlay
reaches the surface through the same door core does.
"""

import asyncio
from unittest.mock import patch

import pytest
from django.test import TestCase
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from teatree.backends.types import Service
from teatree.core.overlay import McpTool, McpToolGroup, OverlayConfig, OverlayConnectors
from teatree.mcp import build_server, write_tools
from teatree.mcp.server import ToolNameCollisionError, declared_write_tool_seams

_READ_ONLY = ToolAnnotations(read_only_hint=True)
_WRITE = ToolAnnotations(read_only_hint=False)

_SEAM = "call_command('demo', …) — the seam the `t3` CLI calls"


def _demo_tool(subject: str) -> str:
    """A stand-in for an overlay's own read, so the whole path is exercised."""
    return subject


def _demo_write(subject: str) -> str:
    """A stand-in for an overlay's own mutation — the case the seam gate exists for."""
    return subject


def _group(*tools: McpTool, requires: frozenset[Service] = frozenset()) -> McpToolGroup:
    return McpToolGroup(
        tools=tools,
        instructions="\n".join(f"- {tool.name}(subject): the overlay's own tool." for tool in tools),
        requires=requires,
    )


_READ_GROUP = _group(McpTool("demo_overlay_tool", _demo_tool, _READ_ONLY))


def _needs_notion() -> McpToolGroup:
    return _group(McpTool("demo_overlay_tool", _demo_tool, _READ_ONLY), requires=frozenset({Service.NOTION}))


class _Contributing(OverlayConnectors):
    def __init__(self, group: McpToolGroup | None = None) -> None:
        self._group = group if group is not None else _READ_GROUP

    def mcp_tool_group(self) -> McpToolGroup | None:
        return self._group


class _Raising(OverlayConnectors):
    def mcp_tool_group(self) -> McpToolGroup | None:
        msg = "the overlay could not resolve its Notion credential"
        raise RuntimeError(msg)


class _Overlay:
    def __init__(self, connectors: OverlayConnectors, *services: Service) -> None:
        self.config = OverlayConfig(required_third_party_services=frozenset(services))
        self.connectors = connectors


def _server_with(overlays: dict[str, _Overlay]) -> MCPServer:
    with patch("teatree.mcp.server.get_all_overlays", return_value=overlays):
        return build_server()


def _server_for(connectors: OverlayConnectors, *services: Service) -> MCPServer:
    return _server_with({"demo": _Overlay(connectors, *services)})


def _tool_names(connectors: OverlayConnectors, *services: Service) -> set[str]:
    return {tool.name for tool in asyncio.run(_server_for(connectors, *services).list_tools())}


class TestOverlayToolGroups(TestCase):
    def test_a_contributed_group_registers_its_tools(self) -> None:
        assert "demo_overlay_tool" in _tool_names(_Contributing())

    def test_a_contributed_group_is_announced_under_its_overlay_name(self) -> None:
        instructions = _server_for(_Contributing()).instructions or ""

        assert "demo_overlay_tool(subject)" in instructions
        assert "demo" in instructions

    def test_the_default_contributes_nothing(self) -> None:
        server = _server_for(OverlayConnectors())

        assert "demo_overlay_tool" not in {tool.name for tool in asyncio.run(server.list_tools())}
        assert "demo_overlay_tool" not in (server.instructions or "")


class TestWriteToolsDeclareTheirSeam(TestCase):
    """The negative direction is the whole control: a seamless write must be REFUSED."""

    def test_a_write_tool_naming_no_seam_never_reaches_the_surface(self) -> None:
        seamless = _group(McpTool("demo_overlay_write", _demo_write, _WRITE))

        assert "demo_overlay_write" not in _tool_names(_Contributing(seamless))

    def test_a_seamless_write_takes_its_whole_group_with_it(self) -> None:
        mixed = _group(
            McpTool("demo_overlay_tool", _demo_tool, _READ_ONLY),
            McpTool("demo_overlay_write", _demo_write, _WRITE),
        )

        assert "demo_overlay_tool" not in _tool_names(_Contributing(mixed))

    def test_a_refused_group_is_not_advertised_either(self) -> None:
        seamless = _group(McpTool("demo_overlay_write", _demo_write, _WRITE))

        assert "demo_overlay_write" not in (_server_for(_Contributing(seamless)).instructions or "")

    def test_a_tool_with_no_read_only_hint_at_all_counts_as_a_write(self) -> None:
        unhinted = _group(McpTool("demo_overlay_unhinted", _demo_write, ToolAnnotations()))

        assert "demo_overlay_unhinted" not in _tool_names(_Contributing(unhinted))

    def test_a_write_tool_that_names_its_seam_registers(self) -> None:
        guarded = _group(McpTool("demo_overlay_write", _demo_write, _WRITE, seam=_SEAM))

        assert "demo_overlay_write" in _tool_names(_Contributing(guarded))

    def test_an_admitted_group_extends_the_seam_allowlist_core_alone_cannot_see(self) -> None:
        guarded = _group(McpTool("demo_overlay_write", _demo_write, _WRITE, seam=_SEAM))

        with patch("teatree.mcp.server.get_all_overlays", return_value={"demo": _Overlay(_Contributing(guarded))}):
            seams = declared_write_tool_seams(frozenset())

        assert seams["demo_overlay_write"] == _SEAM
        assert set(write_tools.TOOL_SEAMS) <= set(seams)


class TestServiceDeclarationGate(TestCase):
    def test_a_group_requiring_an_undeclared_service_contributes_nothing(self) -> None:
        needs_notion = _needs_notion()

        assert "demo_overlay_tool" not in _tool_names(_Contributing(needs_notion))

    def test_the_same_group_registers_once_the_service_is_declared(self) -> None:
        needs_notion = _needs_notion()

        assert "demo_overlay_tool" in _tool_names(_Contributing(needs_notion), Service.NOTION)

    def test_another_overlays_declaration_satisfies_the_requirement(self) -> None:
        needs_notion = _needs_notion()
        server = _server_with(
            {
                "demo": _Overlay(_Contributing(needs_notion)),
                "sibling": _Overlay(OverlayConnectors(), Service.NOTION),
            }
        )

        assert "demo_overlay_tool" in {tool.name for tool in asyncio.run(server.list_tools())}


class TestOneBrokenOverlayDoesNotTakeTheServerDown(TestCase):
    def test_a_raising_hook_costs_only_that_overlay_its_tools(self) -> None:
        server = _server_with({"broken": _Overlay(_Raising()), "healthy": _Overlay(_Contributing())})
        names = {tool.name for tool in asyncio.run(server.list_tools())}

        assert "demo_overlay_tool" in names
        assert "ticket_get" in names


class TestNameCollisionsAreRefusedNotDropped(TestCase):
    def test_a_tool_name_the_server_already_gave_out_is_refused_loudly(self) -> None:
        shadowing = _group(McpTool("ticket_get", _demo_tool, _READ_ONLY))

        with pytest.raises(ToolNameCollisionError, match="ticket_get"):
            _server_for(_Contributing(shadowing))

    def test_two_overlays_claiming_one_name_is_refused_too(self) -> None:
        with pytest.raises(ToolNameCollisionError, match="demo_overlay_tool"):
            _server_with({"a": _Overlay(_Contributing()), "b": _Overlay(_Contributing())})
