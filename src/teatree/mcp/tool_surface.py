"""The registration boundary for read-only withholding and write approvals."""

from collections.abc import Callable, Mapping
from inspect import getdoc, signature
from typing import Any, override

from mcp.server.mcpserver import MCPServer
from mcp.types import Icon, ToolAnnotations

from teatree.core.mcp_tool_group import ApprovalTier, WriteSeam


class ToolNameCollisionError(RuntimeError):
    """A second tool claimed a name already registered on this server."""


class ToolContractError(RuntimeError):
    """A registered tool has no safe approval contract."""


class _NoShadowServer(MCPServer):
    """Refuse duplicate names that MCPServer would otherwise silently discard."""

    def __init__(
        self,
        name: str,
        *,
        instructions: str,
        read_only: bool,
        allowed_writes: frozenset[str],
        write_seams: Mapping[str, WriteSeam],
    ) -> None:
        super().__init__(name, instructions=instructions)
        self.registered_names: set[str] = set()
        self.read_only = read_only
        self.allowed_writes = allowed_writes
        self.write_seams = write_seams

    @override
    def add_tool(
        self,
        fn: Callable[..., Any],
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        annotations: ToolAnnotations | None = None,
        icons: list[Icon] | None = None,
        meta: dict[str, Any] | None = None,
        structured_output: bool | None = None,
    ) -> None:
        tool_name = name or getattr(fn, "__name__", "")
        is_read = bool(annotations and annotations.read_only_hint)
        if self.read_only and not is_read and tool_name not in self.allowed_writes:
            return
        if tool_name and tool_name in self.registered_names:
            msg = f"MCP tool name {tool_name!r} is already registered; a second registration would be silently dropped."
            raise ToolNameCollisionError(msg)
        tier = ApprovalTier.READ if is_read else self._write_tier(tool_name)
        if annotations and annotations.destructive_hint and tier is ApprovalTier.POSTURE:
            msg = f"MCP tool {tool_name!r} is destructive and cannot use the posture tier"
            raise ToolContractError(msg)
        dry_run = signature(fn).parameters.get("dry_run")
        if dry_run is not None and dry_run.default is False:
            msg = f"MCP tool {tool_name!r} defaults dry_run to false"
            raise ToolContractError(msg)
        if tool_name:
            self.registered_names.add(tool_name)
        super().add_tool(
            fn,
            name=name,
            title=title,
            description=f"[approval: {tier.value}] {description or getdoc(fn) or ''}",
            annotations=annotations,
            icons=icons,
            meta={**(meta or {}), "teatree/approval_tier": tier.value},
            structured_output=structured_output,
        )

    def _write_tier(self, name: str) -> ApprovalTier:
        seam = self.write_seams.get(name)
        if seam is None or not seam.gate.strip() or seam.tier is ApprovalTier.READ:
            msg = f"MCP write tool {name!r} has no gated approval tier"
            raise ToolContractError(msg)
        return seam.tier


_READ_ONLY_NOTICE = (
    "\n\nThis server is read-only for this dispatch. Write tools registered: {writes}. "
    "Report unmet writes in the result envelope."
)


def _read_only_instructions(instructions: str, *, withheld: frozenset[str], allowed_writes: frozenset[str]) -> str:
    withheld_names = withheld - allowed_writes
    withheld_lines = tuple(f"- {name}(" for name in withheld_names)
    sections: list[str] = []
    for section in instructions.split("\n\n"):
        lines = section.split("\n")
        kept = []
        for line in lines:
            if line.startswith(withheld_lines):
                continue
            if line.startswith("Mailbox tools: "):
                names = line.removeprefix("Mailbox tools: ").split(", ")
                kept.append("Mailbox tools: " + ", ".join(name for name in names if name not in withheld_names))
            else:
                kept.append(line)
        if len(kept) == len(lines) or any(line.startswith("- ") for line in kept):
            sections.append("\n".join(kept))
    return "\n\n".join(sections) + _READ_ONLY_NOTICE.format(writes=", ".join(sorted(allowed_writes)) or "none")
