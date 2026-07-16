"""Slack MCP tool group — reads + one gated reaction write (#3076).

Registered only when a registered overlay declares ``Service.SLACK``. The
messaging client is resolved through
:func:`teatree.core.backend_factory.configured_messaging_from_overlay` (a core
seam that returns ``None`` for a noop-messaging declarer so the resolver reaches
the credentialed overlay — #3299), never a direct ``teatree.backends.slack``
import, so the transport-boundary fitness test holds. The reads stay read-only; the one write (``slack_react``) routes
through :class:`~teatree.core.on_behalf_egress.OnBehalfSlackEgress` — the single
colleague-surface Slack egress owner — so the #117 send-proxy, the on-behalf
approval gate, and the after-post notify receipt all fire (a self-DM reaction is
ungated by design; a colleague-surface reaction with no recorded approval is
refused). All other posting stays on the gated ``t3`` / review surfaces.
"""

from typing import Any

from asgiref.sync import sync_to_async
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from teatree.backends.types import Service
from teatree.core.backend_factory import configured_messaging_from_overlay
from teatree.core.backend_protocols import MessagingBackend
from teatree.core.on_behalf_egress import OnBehalfPostBlockedError, OnBehalfSlackEgress
from teatree.mcp.service_resolver import resolve_declaring_overlay_client

_READ_ONLY = ToolAnnotations(readOnlyHint=True)
_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)

INSTRUCTIONS = (
    "- slack_mentions(since): recent @-mentions of the user.\n"
    "- slack_channel_history(channel, limit): recent messages in a channel.\n"
    "- slack_thread_replies(channel, thread_ts): replies under one thread.\n"
    "- slack_permalink(channel, ts): the permalink for one message.\n"
    "- slack_react(channel, ts, emoji): add a reaction. A self-DM reaction is "
    "ungated; a colleague/channel reaction goes through the on-behalf gate and "
    "returns ok=false + a `blocked` remediation when no approval is recorded."
)


def _client() -> MessagingBackend:
    # ``configured_messaging_from_overlay`` (not ``messaging_from_overlay``) so a
    # noop-messaging overlay that declares ``Service.SLACK`` without credentials
    # is skipped and the resolver reaches the overlay that has them (#3299).
    return resolve_declaring_overlay_client(
        Service.SLACK, configured_messaging_from_overlay, description="Slack messaging backend"
    )


async def _slack_mentions(*, since: str = "") -> list[dict[str, Any]]:
    return await sync_to_async(lambda: _client().fetch_mentions(since=since), thread_sensitive=True)()


async def _slack_channel_history(channel: str, *, limit: int = 50) -> list[dict[str, Any]]:
    return await sync_to_async(
        lambda: _client().fetch_channel_history(channel=channel, limit=limit), thread_sensitive=True
    )()


async def _slack_thread_replies(channel: str, thread_ts: str) -> list[dict[str, Any]]:
    return await sync_to_async(
        lambda: _client().fetch_thread_replies(channel=channel, thread_ts=thread_ts), thread_sensitive=True
    )()


async def _slack_permalink(channel: str, ts: str) -> str:
    return await sync_to_async(lambda: _client().get_permalink(channel=channel, ts=ts), thread_sensitive=True)()


async def _slack_react(channel: str, ts: str, emoji: str) -> dict[str, Any]:
    """Add an emoji reaction, gated on a colleague surface, ungated for a self-DM.

    Routes through :class:`OnBehalfSlackEgress` so a colleague/channel reaction
    runs the #117 send-proxy + the on-behalf approval gate + the after-post
    notify receipt; a self-DM (the user's own DM) is ungated. A blocked
    colleague reaction returns ``ok=false`` and a ``blocked`` remediation string
    instead of crashing the tool call.
    """

    def _react() -> dict[str, Any]:
        name = emoji.strip().strip(":")
        try:
            response = OnBehalfSlackEgress(_client()).react(
                channel=channel,
                ts=ts,
                emoji=name,
                target=channel,
                action="mcp_slack_react",
                destination=channel,
            )
        except OnBehalfPostBlockedError as blocked:
            return {"ok": False, "blocked": str(blocked)}
        return {"ok": bool(response.get("ok")), "channel": channel, "ts": ts, "response": response}

    return await sync_to_async(_react, thread_sensitive=True)()


def register(server: FastMCP) -> None:
    server.add_tool(_slack_mentions, name="slack_mentions", annotations=_READ_ONLY)
    server.add_tool(_slack_channel_history, name="slack_channel_history", annotations=_READ_ONLY)
    server.add_tool(_slack_thread_replies, name="slack_thread_replies", annotations=_READ_ONLY)
    server.add_tool(_slack_permalink, name="slack_permalink", annotations=_READ_ONLY)
    server.add_tool(_slack_react, name="slack_react", annotations=_WRITE)
