"""What an overlay contributes to the teatree MCP server, and on what terms.

Declarative rather than a bare registrar callback: the server has to judge a
contributed tool BEFORE it reaches the agent surface — is it a write, and if so
which gated seam does it wrap — and a callback that registers whatever it likes
leaves nothing to judge until the tool is already registered. So the group hands
over its tools, and the server does the registering.

The two declarations are the ones the built-in groups already answer for
themselves: ``requires`` is the per-service fail-closed gate
(``OverlayConfig.required_third_party_services``), and ``McpTool.seam`` is the
overlay half of :data:`teatree.mcp.write_tools.TOOL_SEAMS`.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from teatree.backends.types import Service

if TYPE_CHECKING:
    from mcp.types import ToolAnnotations


@dataclass(frozen=True)
class McpTool:
    """One tool an overlay contributes, with the gated seam a write of it wraps.

    ``seam`` names that seam the way ``TOOL_SEAMS`` does — the core function the
    handler calls, and the gates it carries. It is blank for a read.
    """

    name: str
    handler: Callable[..., Any]
    annotations: "ToolAnnotations"
    seam: str = ""

    @property
    def is_write(self) -> bool:
        """Whether this tool mutates anything — an absent read-only hint counts as one."""
        return not (self.annotations and self.annotations.read_only_hint)

    @property
    def declares_its_seam(self) -> bool:
        return not self.is_write or bool(self.seam.strip())


@dataclass(frozen=True)
class McpToolGroup:
    """An overlay's own MCP tools, on the same fail-closed terms as a built-in group.

    ``requires`` names the third-party services the group's tools actually talk
    to; the server registers the group only when every one of them is declared,
    exactly as it gates its own per-service groups. ``instructions`` is the prose
    the server appends under the overlay's name — a tool the instructions never
    mention is a tool no agent reaches for, so the group is registered whole or
    not at all.
    """

    tools: tuple[McpTool, ...]
    instructions: str
    requires: frozenset[Service] = frozenset()

    @property
    def tool_names(self) -> frozenset[str]:
        return frozenset(tool.name for tool in self.tools)

    @property
    def undeclared_write_tools(self) -> tuple[str, ...]:
        """The write tools naming no seam — each one an ungated mutation on the agent surface."""
        return tuple(tool.name for tool in self.tools if not tool.declares_its_seam)

    @property
    def seams(self) -> dict[str, str]:
        """This group's ``TOOL_SEAMS`` contribution — every tool that names a seam."""
        return {tool.name: tool.seam for tool in self.tools if tool.seam.strip()}
