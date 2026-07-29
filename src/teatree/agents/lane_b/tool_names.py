"""Lane B's model-visible tool names — the skill / SDK vocabulary (the Lane-B boundary).

teatree's per-phase least-privilege table (:mod:`teatree.core.modelkit.phase_tools`)
is keyed on provider-neutral CAPABILITY names (``read_file``, ``shell``, …). A loaded
skill speaks the Claude Code / SDK vocabulary (``Read``, ``Bash``, …), and Lane A
already maps neutral -> SDK names at its boundary (:mod:`teatree.agents.sdk_tool_map`).
This module is Lane B's SYMMETRIC boundary: it maps each neutral capability to the
SINGLE display name Lane B's ``FunctionToolset`` exposes to the model, so a skill
instruction that says ``Bash`` / ``Read`` names the actual Lane B tool.

Every display name here is the PRIMARY SDK name for the same capability in
:data:`~teatree.agents.sdk_tool_map.CAPABILITY_TO_SDK_TOOLS`, so both lanes speak one
vocabulary (pinned by the Lane-A parity test).
"""

from typing import Final

TOOL_READ: Final = "Read"
TOOL_WRITE: Final = "Write"
TOOL_EDIT: Final = "Edit"
TOOL_GREP: Final = "Grep"
TOOL_BASH: Final = "Bash"

#: Neutral capability name -> the model-visible Lane B tool name (the skill vocabulary).
#: Only the capabilities Lane B builds as ``FunctionToolset`` tools appear here; a
#: capability with no Lane B tool (``web_*``, ``dispatch_subtask``, the MCP-served
#: reads) resolves to itself via :func:`lane_b_tool_name`.
CAPABILITY_TO_LANE_B_TOOL: Final[dict[str, str]] = {
    "read_file": TOOL_READ,
    "write_file": TOOL_WRITE,
    "edit_file": TOOL_EDIT,
    "search_files": TOOL_GREP,
    "shell": TOOL_BASH,
}


def lane_b_tool_name(capability: str) -> str:
    """The model-visible Lane B tool name for a neutral *capability* (identity if unmapped)."""
    return CAPABILITY_TO_LANE_B_TOOL.get(capability, capability)
