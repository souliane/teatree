"""The escalate tool as a Codex dynamic tool: its schema; the call is the shared escalation (#5685)."""

from typing import Any

from teatree.agents.coordinator_escalation import ESCALATE
from teatree.core.answering.escalation import KINDS

ESCALATE_SPEC: tuple[str, str, dict[str, Any], list[str]] = (
    ESCALATE,
    "Ask the coordinator for help: one internal request that waits for an answer, then may reach the owner.",
    {
        "kind": {"type": "string", "enum": list(KINDS)},
        "message": {"type": "string", "description": "The one question, in plain words."},
    },
    ["kind", "message"],
)
