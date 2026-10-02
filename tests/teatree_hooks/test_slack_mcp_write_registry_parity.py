"""The egress guard classifies every Slack MCP write tool the repo already knows about.

``quote_scanner`` carries the explicit connector roster; ``mcp_slack_write_guard``
is cold-import-safe (stdlib-only module top) so it cannot read that roster at
runtime. This binding is what keeps its verb classifier from drifting behind the
roster again — a new registry entry whose verb the guard does not recognise turns
this red. The direction is one-way on purpose: the guard must block a SUPERSET of
the roster (``chat_delete`` is a write no roster entry names).
"""

from hooks.scripts.mcp_slack_write_guard import is_slack_mcp_write
from teatree.hooks.quote_scanner import _SLACK_MCP_WRITE_TOOLS


class TestGuardCoversTheWriteToolRegistry:
    def test_every_registry_tool_classifies_as_a_write(self) -> None:
        unclassified = [
            tool for tool in _SLACK_MCP_WRITE_TOOLS if not is_slack_mcp_write(f"mcp__claude_ai_Slack__{tool}")
        ]
        assert unclassified == []
