"""The escalate tool as a Codex dynamic tool: its schema and the call that reaches the shared escalation (#5685)."""

from typing import Any

from teatree.agents.coordinator_escalation import ESCALATE, escalate
from teatree.agents.live_mailbox_client import MailboxClient
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


async def call_escalate(mailbox: MailboxClient, *, kind: str, message: str) -> dict[str, object]:
    return dict(await escalate(mailbox, kind=kind, message=message))
