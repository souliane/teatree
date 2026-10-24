"""The MCP write tool for the terminal relay: the same seam as ``t3 <overlay> pending_chat relay``."""

from typing import Any

from asgiref.sync import sync_to_async

from teatree.mcp.write_tool_run import run_emitting_command


async def pending_chat_relay(text: str, relay_id: str, *, session_id: str = "") -> dict[str, Any]:
    """Relay an owner prompt verbatim into the owner's factory Slack DM and queue it for the slack_answer cycle.

    Use it while factory Slack is live. The result carries ``marker``: ``t3-relay-receipt: …`` when relayed,
    ``t3-relay-refused: …`` when the prompt must be answered in the terminal instead. A retry with the same
    ``relay_id`` posts nothing new.
    """
    return await sync_to_async(
        lambda: run_emitting_command(
            "pending_chat",
            "relay",
            f"--relay-id={relay_id}",
            f"--text={text}",
            f"--session-id={session_id}",
            "--json",
        ),
        thread_sensitive=True,
    )()
