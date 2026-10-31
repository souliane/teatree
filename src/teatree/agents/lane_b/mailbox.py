"""Native mailbox tools for a task-owned Lane B identity."""

from collections.abc import Awaitable
from typing import Annotated

from pydantic import Field
from pydantic_ai.toolsets.function import FunctionToolset

from teatree.agents.coordinator_escalation import ESCALATE, escalate
from teatree.agents.live_mailbox_client import MailboxClient, MailboxError
from teatree.core.answering.escalation import EscalationRefusedError
from teatree.core.modelkit.phase_tools import MCP_WRITE, mcp_write_tools_for_phase, tools_for_phase


async def _result(request: Awaitable[object]) -> object:
    try:
        return await request
    except (MailboxError, EscalationRefusedError) as exc:
        return {"error": str(exc)}


def build_mailbox_toolset(client: MailboxClient, phase: str) -> FunctionToolset[None]:
    allowed = tools_for_phase(phase)
    toolset: FunctionToolset[None] = FunctionToolset(
        instructions=(
            "A mailbox receipt is acceptance, not obedience. "
            "After not_accepted_in_time, retry with the same key; a new key sends a second copy."
        )
    )
    names = (
        {"agent_mailbox_peers", "agent_mailbox_send", "agent_mailbox_inbox", "agent_mailbox_wait", ESCALATE}
        if MCP_WRITE in allowed
        else {"agent_mailbox_peers", *mcp_write_tools_for_phase(phase)}
    )

    async def peers() -> object:
        """List live TeaTree tasks on this host."""
        return await _result(client.peers())

    async def send(
        to: str,
        text: str,
        key: Annotated[str, Field(description="Reuse the same key on retries to avoid duplicate messages.")],
    ) -> object:
        """Send a keyed message to one peer and await its receipt for at most 20 seconds."""
        return await _result(client.send(to, text, key))

    async def inbox(after_id: int = 0, limit: int = 50) -> object:
        """Read and acknowledge messages addressed to this task."""
        return await _result(client.inbox(after_id=after_id, limit=limit))

    async def wait(after_id: int = 0, timeout_seconds: float = 20) -> object:
        """Wait briefly for mail and acknowledge what is returned."""
        return await _result(client.wait(after_id=after_id, timeout_seconds=timeout_seconds))

    async def escalate_to_coordinator(kind: str, message: str) -> object:
        """Ask the coordinator for help: one internal request that waits for an answer, then may reach the owner.

        kind is internal, or the decision needed: credentials, money_or_plan, public_post, irreversible,
        architecture, product_scope. message is the one question in plain words. A repeat returns the same request.
        """
        return await _result(escalate(client, kind=kind, message=message))

    functions = {
        "agent_mailbox_peers": peers,
        "agent_mailbox_send": send,
        "agent_mailbox_inbox": inbox,
        "agent_mailbox_wait": wait,
        ESCALATE: escalate_to_coordinator,
    }
    for name, function in functions.items():
        if name in names:
            toolset.add_function(function, takes_ctx=False, name=name)
    return toolset
