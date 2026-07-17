"""Verify teatree's own plugin-bundled structured-search MCP server (#2863).

``t3 mcp serve`` (:mod:`teatree.mcp.server`, built under #1023) is registered
via a plugin-bundled ``.mcp.json`` at the repo root — the same convention
official Claude Code plugins use (a flat or ``mcpServers``-wrapped map of
server name to launch command, sitting beside ``.claude-plugin/``). Claude
Code starts plugin-bundled MCP servers automatically once the plugin is
enabled (``t3 setup`` already handles that via
:class:`~teatree.cli.setup.plugin_registrar.PluginRegistrar`) — nothing further
is required to make the tools reachable.

What *is* required is catching drift: a hand-edited or accidentally-deleted
``.mcp.json`` would silently leave agents shelling out to the CLI again
instead of calling the structured-search tools, with no loud signal. This
module is the single chokepoint both ``t3 setup``
(:mod:`teatree.cli.setup.mcp_registrar`) and ``t3 doctor check``
(``_check_teatree_mcp_registration`` in :mod:`teatree.cli.doctor.checks_mcp`)
read, so the two surfaces can never drift on what "correctly registered"
means.
"""

import json
from dataclasses import dataclass
from pathlib import Path

MCP_JSON_FILENAME = ".mcp.json"
TEATREE_MCP_SERVER_NAME = "teatree"
EXPECTED_COMMAND = "t3"
EXPECTED_ARGS = ("mcp", "serve")


@dataclass(frozen=True, slots=True)
class McpRegistrationOutcome:
    """The result of verifying teatree's own MCP server registration."""

    ok: bool
    message: str


def mcp_json_path(repo: Path) -> Path:
    """The path a teatree clone's plugin-bundled ``.mcp.json`` lives at."""
    return repo / MCP_JSON_FILENAME


def read_declared_mcp_servers(path: Path) -> dict[str, dict]:
    """The ``{server_name: launch_config}`` map declared at *path*.

    Accepts both shapes Claude Code's plugin ``.mcp.json`` loader tolerates —
    a flat map (``{"teatree": {...}}``, the shape most official marketplace
    plugins ship) and one wrapped in a top-level ``mcpServers`` key (the shape
    the plugin-reference docs show, and the shape teatree itself ships). A
    missing, unreadable, or malformed file reads as "no servers declared"
    (``{}``) rather than raising — the caller decides what an empty result
    means for its own OK/WARN/FAIL posture.
    """
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    wrapped = data.get("mcpServers")
    servers = wrapped if isinstance(wrapped, dict) else data
    return {name: cfg for name, cfg in servers.items() if isinstance(cfg, dict)}


def verify_teatree_mcp_registration(repo: Path) -> McpRegistrationOutcome:
    """Verify *repo* ships a well-formed ``teatree`` entry in ``.mcp.json``.

    Structural only — no live probe. A ``t3 doctor check`` caller layers a
    live ``claude mcp list`` probe (:mod:`teatree.core.mcp_connectivity`) on
    top of this; ``t3 setup`` uses this alone (setup has no reason to shell
    out to ``claude`` — it only needs to confirm the file it ships is intact).
    """
    path = mcp_json_path(repo)
    entry = read_declared_mcp_servers(path).get(TEATREE_MCP_SERVER_NAME)
    if entry is None:
        return McpRegistrationOutcome(
            ok=False,
            message=(
                f"{path} does not declare the '{TEATREE_MCP_SERVER_NAME}' MCP server. "
                "Agents fall back to shelling out to the t3 CLI for structured reads instead "
                "of calling the read-only search tools."
            ),
        )
    command = entry.get("command")
    args = tuple(entry.get("args") or ())
    if command != EXPECTED_COMMAND or args != EXPECTED_ARGS:
        return McpRegistrationOutcome(
            ok=False,
            message=(
                f"{path} declares '{TEATREE_MCP_SERVER_NAME}' as {command!r} {list(args)} — "
                f"expected {EXPECTED_COMMAND!r} {list(EXPECTED_ARGS)}."
            ),
        )
    return McpRegistrationOutcome(
        ok=True,
        message=(
            f"MCP server '{TEATREE_MCP_SERVER_NAME}' registered via {path} "
            "(plugin-bundled; Claude Code starts it once the t3 plugin is enabled)."
        ),
    )


__all__ = [
    "EXPECTED_ARGS",
    "EXPECTED_COMMAND",
    "MCP_JSON_FILENAME",
    "TEATREE_MCP_SERVER_NAME",
    "McpRegistrationOutcome",
    "mcp_json_path",
    "read_declared_mcp_servers",
    "verify_teatree_mcp_registration",
]
