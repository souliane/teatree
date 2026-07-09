"""Fitness tests for the MCP transport boundary (#3076).

Two structural invariants of the MCP-serves-overlay-services architecture:

No transport imports — an MCP handler never touches a forge/messaging transport
directly. No module under ``teatree.mcp`` may import the concrete backends
(``github`` / ``gitlab`` / ``slack`` / ``msteams`` / ``figma``), the merge RPC
transport, or ``subprocess``. Writes reach transports only through core seams
(``call_command``, the review seam), which own the gates. tach's layer model
cannot pin this (lower layers are implicitly importable), so this AST walk is
the enforcement.

Seam-allowlist coverage — every registered non-read-only tool must name its seam
in ``write_tools.TOOL_SEAMS``, so a new write tool cannot land without declaring
which gated seam it wraps.
"""

import ast
import asyncio
from pathlib import Path

import teatree.mcp
from teatree.mcp import build_server, write_tools

_MCP_DIR = Path(teatree.mcp.__file__).parent

_FORBIDDEN_IMPORT_PREFIXES = (
    "teatree.backends.github",
    "teatree.backends.gitlab",
    "teatree.backends.slack",
    "teatree.backends.msteams",
    "teatree.backends.figma",
    "teatree.backends.forge_merge_rpc",
    "subprocess",
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
            f"{path.name}: {module}"
            for path in sorted(_MCP_DIR.glob("*.py"))
            for module in sorted(_imported_modules(path))
            if module.startswith(_FORBIDDEN_IMPORT_PREFIXES)
        ]

        assert not offenders, f"MCP handlers must reach transports through core seams only: {offenders}"


class TestSeamAllowlistCoverage:
    def test_every_write_tool_declares_its_seam(self) -> None:
        tools = asyncio.run(build_server().list_tools())
        write_tool_names = {tool.name for tool in tools if not (tool.annotations and tool.annotations.readOnlyHint)}

        undeclared = write_tool_names - set(write_tools.TOOL_SEAMS)
        assert not undeclared, f"write tools without a declared seam: {sorted(undeclared)}"

    def test_seam_map_carries_no_stale_entries(self) -> None:
        tools = asyncio.run(build_server().list_tools())
        registered = {tool.name for tool in tools}

        stale = set(write_tools.TOOL_SEAMS) - registered
        assert not stale, f"TOOL_SEAMS names unregistered tools: {sorted(stale)}"
