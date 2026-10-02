"""The external-connector concern of an overlay — ``overlay.connectors``."""

from collections.abc import Callable
from typing import TYPE_CHECKING

from teatree.core.mcp_tool_group import McpToolGroup

if TYPE_CHECKING:
    from teatree.core.connector_manifest import ConnectorRequirement


class OverlayConnectors:
    """External-connector concern (claude.ai, MCP, Slack/Notion) — ``overlay.connectors``."""

    def preflight(self) -> list[Callable[[], None]]:
        """Zero-arg probes run before any connector-dependent loop work."""
        from teatree.core.connector_probes import standard_probes  # noqa: PLC0415 — deferred: avoids import cycle

        return standard_probes(self.manifest(), self.mcp_provider_expectations())

    def mcp_provider_expectations(self) -> dict[str, str]:
        """``{mcp_server_name: provider}`` for the #2282 connectivity check; default empty."""
        return {}

    def mcp_tool_group(self) -> McpToolGroup | None:
        """The overlay's own tools for the teatree MCP server; none by default.

        The group is registered only on the terms it declares: every service in
        ``requires`` declared by some overlay, and every write tool naming its
        gated seam.
        """
        return None

    def manifest(self) -> list["ConnectorRequirement"]:
        """Overlay's required-vs-optional claude.ai connectors by NAME; default none (PR-19)."""
        return []
