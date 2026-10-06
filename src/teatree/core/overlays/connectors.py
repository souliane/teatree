"""The teatree-MCP concern of an overlay — ``overlay.connectors``."""

from collections.abc import Callable

from teatree.core.mcp_tool_group import McpToolGroup


class OverlayConnectors:
    """What an overlay contributes to, and checks before, the work its services serve."""

    def preflight(self) -> list[Callable[[], None]]:
        """Zero-arg probes run before any service-dependent loop work; one raising ``RuntimeError`` refuses the tick."""
        return []

    def mcp_tool_group(self) -> McpToolGroup | None:
        """The overlay's own tools for the teatree MCP server; none by default.

        The group is registered only on the terms it declares: every service in
        ``requires`` declared by some overlay, and every write tool naming its
        gated seam.
        """
        return None
