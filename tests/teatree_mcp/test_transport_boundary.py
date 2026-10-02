"""Fitness tests for the MCP transport boundary (#3076).

Two structural invariants of the MCP-serves-overlay-services architecture:

No transport imports — an MCP handler never touches a forge/messaging transport
directly. No module under ``teatree.mcp`` may import a concrete backend
(``github`` / ``gitlab`` / ``slack`` / ``msteams`` / ``figma`` / ``sentry`` /
``sharepoint`` / ``notion``), the merge RPC transport, ``subprocess``, or a raw
HTTP/SDK client
(``httpx`` / ``requests`` / ``slack_sdk`` / ``urllib.request``) the concrete
backends name. Writes reach transports only through core seams (``call_command``,
the review seam, the ``backend_factory`` client builders), which own the gates.
tach's layer model cannot pin this (lower layers are implicitly importable), so
this recursive AST walk is the enforcement.

Seam-allowlist coverage — every registered non-read-only tool must name its seam,
so a new write tool cannot land without declaring which gated seam it wraps. The
allowlist spans BOTH halves: ``write_tools.TOOL_SEAMS`` is core's, and each
admitted overlay group contributes its own. Reading core's half alone leaves an
overlay's write tools outside the guard entirely, which is why the fixture below
contributes a group rather than the bare default that declares nothing.
"""

import ast
import asyncio
from pathlib import Path
from unittest.mock import patch

from mcp.types import ToolAnnotations

import teatree.mcp
from teatree.backends.types import Service
from teatree.core.overlay import McpTool, McpToolGroup, OverlayConfig, OverlayConnectors
from teatree.mcp import build_server
from teatree.mcp.server import declared_write_tool_seams

_MCP_DIR = Path(teatree.mcp.__file__).parent

_OVERLAY_SERVICES = frozenset({Service.GITHUB, Service.GITLAB, Service.SLACK})


def _overlay_write(subject: str) -> str:
    """An overlay's own mutation — the tool class the seam allowlist has to cover."""
    return subject


class _ContributingConnectors(OverlayConnectors):
    def mcp_tool_group(self) -> McpToolGroup | None:
        return McpToolGroup(
            tools=(
                McpTool(
                    "overlay_demo_write",
                    _overlay_write,
                    ToolAnnotations(read_only_hint=False),
                    seam="call_command('demo', …) — the seam the `t3` CLI calls",
                ),
            ),
            instructions="- overlay_demo_write(subject): the overlay's own gated write.",
        )


class _AllForgeOverlay:
    """Declares every forge + slack service, and contributes a write tool of its own."""

    def __init__(self) -> None:
        self.config = OverlayConfig(required_third_party_services=_OVERLAY_SERVICES)
        self.connectors = _ContributingConnectors()


_FORBIDDEN_IMPORT_PREFIXES = (
    "teatree.backends.github",
    "teatree.backends.gitlab",
    "teatree.backends.slack",
    "teatree.backends.msteams",
    "teatree.backends.figma",
    "teatree.backends.sentry",
    "teatree.backends.sharepoint",
    "teatree.backends.notion",
    "teatree.backends.forge_merge_rpc",
    "subprocess",
    "httpx",
    "requests",
    "slack_sdk",
    "urllib.request",
)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


class TestNoTransportImports:
    def test_mcp_modules_never_import_a_transport(self) -> None:
        offenders = [
            f"{path.relative_to(_MCP_DIR)}: {module}"
            for path in sorted(_MCP_DIR.rglob("*.py"))
            for module in sorted(_imported_modules(path))
            if module.startswith(_FORBIDDEN_IMPORT_PREFIXES)
        ]

        assert not offenders, f"MCP handlers must reach transports through core seams only: {offenders}"


class TestSeamAllowlistCoverage:
    # Built against a server that declares github + gitlab + slack, so every
    # conditionally-registered per-service write tool (the forge issue writes,
    # slack_react) is present — otherwise a forge write tool would look "stale"
    # in an env that happens not to declare its forge.
    def test_every_write_tool_declares_its_seam(self) -> None:
        with patch("teatree.mcp.server.get_all_overlays", return_value={"a": _AllForgeOverlay()}):
            tools = asyncio.run(build_server().list_tools())
            declared = declared_write_tool_seams(_OVERLAY_SERVICES)
        write_tool_names = {tool.name for tool in tools if not (tool.annotations and tool.annotations.read_only_hint)}

        undeclared = write_tool_names - set(declared)
        assert not undeclared, f"write tools without a declared seam: {sorted(undeclared)}"

    def test_the_guard_spans_the_overlay_half_of_the_surface(self) -> None:
        with patch("teatree.mcp.server.get_all_overlays", return_value={"a": _AllForgeOverlay()}):
            tools = asyncio.run(build_server().list_tools())
            declared = declared_write_tool_seams(_OVERLAY_SERVICES)

        assert "overlay_demo_write" in {tool.name for tool in tools}
        assert "overlay_demo_write" in declared

    def test_seam_map_carries_no_stale_entries(self) -> None:
        with patch("teatree.mcp.server.get_all_overlays", return_value={"a": _AllForgeOverlay()}):
            registered = {tool.name for tool in asyncio.run(build_server().list_tools())}
            declared = declared_write_tool_seams(_OVERLAY_SERVICES)

        stale = set(declared) - registered
        assert not stale, f"the seam allowlist names unregistered tools: {sorted(stale)}"
