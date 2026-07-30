"""Assemble the phase-scoped, gated toolset list Lane B hands to its ``Agent``.

The one place the capability toolsets, the phase-scoping filter, the hard-deny
wrapper, the soft-gate approval, and the MCP toolsets are composed into the list
``PydanticAiHarness`` passes as ``Agent(toolsets=...)``. Composition order (inner
to outer): capabilities → phase filter → hard-deny wrapper → soft-gate approval;
the read-only MCP toolsets ride alongside, EXCEPT for an empty (non-``None``)
phase allowance — a ``_NONE`` phase (``short_describe``, ``directive_reading``)
may call nothing, so no MCP attaches past the phase filter.
"""

from dataclasses import dataclass

from pydantic_ai.toolsets.abstract import AbstractToolset
from pydantic_ai.toolsets.combined import CombinedToolset

from teatree.agents.lane_b.config import LaneBToolConfig
from teatree.agents.lane_b.filesystem import build_filesystem_toolset
from teatree.agents.lane_b.gating import DEFAULT_SOFT_GATED, HardDenyToolset, make_soft_gate_predicate
from teatree.agents.lane_b.mcp import build_mcp_toolsets
from teatree.agents.lane_b.shell import build_shell_toolset
from teatree.agents.lane_b.tool_names import lane_b_tool_name
from teatree.core.modelkit.phase_tools import tools_for_phase


@dataclass(frozen=True)
class LaneBToolsets:
    """The assembled toolsets plus whether the deferred-approval output is needed.

    ``needs_deferred_output`` is ``True`` only when a soft gate is configured — the
    ``Agent`` then adds ``DeferredToolRequests`` to its ``output_type`` so an
    approval-required call surfaces a parkable deferred request. Empty soft-gate
    set (the default) keeps a pure text ``output_type``, byte-identical to today.
    """

    toolsets: list[AbstractToolset[None]]
    needs_deferred_output: bool


def build_lane_b_toolsets(config: LaneBToolConfig, *, soft_gated: frozenset[str] = DEFAULT_SOFT_GATED) -> LaneBToolsets:
    """Compose the capability + MCP toolsets for *config*'s phase and worktree.

    A phase names an allowed tool set (:func:`tools_for_phase`); the Shell tool is
    only built when ``shell`` is allowed and the File System write tools only when
    ``write_file`` is allowed, so a read-only phase's toolset carries no mutation
    surface even before the runtime filter. The composed capabilities are wrapped
    in :class:`HardDenyToolset` (the shared-registry hard-deny) and, when a soft
    gate is configured, in the native ``approval_required`` deferral. The read-only
    MCP toolsets are appended un-filtered — UNLESS the phase allowance is a
    non-``None`` empty set (a ``_NONE`` phase), which exposes nothing and so gets
    no MCP either.
    """
    allowed = tools_for_phase(config.phase) if config.phase else None
    capability_toolsets: list[AbstractToolset[None]] = []

    if config.fs_root is not None:
        allow_write = allowed is None or "write_file" in allowed
        capability_toolsets.append(build_filesystem_toolset(config.fs_root, allow_write=allow_write))

    if allowed is None or "shell" in allowed:
        capability_toolsets.append(build_shell_toolset(config))

    combined: AbstractToolset[None] = CombinedToolset(capability_toolsets)
    if allowed is not None:
        # The allowance is in NEUTRAL capability names; the assembled tool_defs carry
        # the model-visible skill names (Bash/Read/…). Map the allowance UP to the
        # display vocabulary so the filter compares like with like.
        allowed_names = {lane_b_tool_name(capability) for capability in allowed}
        combined = combined.filtered(lambda _ctx, tool_def: tool_def.name in allowed_names)

    gated: AbstractToolset[None] = HardDenyToolset(combined, cwd=config.fs_root)
    if soft_gated:
        gated = gated.approval_required(make_soft_gate_predicate(soft_gated))

    # A _NONE-phase allowance (empty, but NOT None) means the phase may call
    # nothing — so no MCP toolset may attach past the phase filter either.
    mcp_toolsets: list[AbstractToolset[None]] = [] if allowed is not None and not allowed else build_mcp_toolsets()
    toolsets: list[AbstractToolset[None]] = [gated, *mcp_toolsets]
    return LaneBToolsets(toolsets=toolsets, needs_deferred_output=bool(soft_gated))
