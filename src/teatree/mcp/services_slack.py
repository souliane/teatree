"""Slack read-only MCP tool group (#3076).

Registered only when a registered overlay declares ``Service.SLACK``. The
messaging client is resolved through
:func:`teatree.core.backend_factory.messaging_from_overlay` (a core seam), never
a direct ``teatree.backends.slack`` import, so the transport-boundary fitness
test holds. Read-only: posting stays on the gated ``t3`` / review surfaces.
"""

from typing import Any

from asgiref.sync import sync_to_async
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from teatree.backends.types import Service
from teatree.core.backend_factory import messaging_from_overlay
from teatree.core.backend_protocols import MessagingBackend
from teatree.core.overlay_loader import get_all_overlays

_READ_ONLY = ToolAnnotations(readOnlyHint=True)

INSTRUCTIONS = (
    "- slack_mentions(since): recent @-mentions of the user.\n"
    "- slack_channel_history(channel, limit): recent messages in a channel.\n"
    "- slack_thread_replies(channel, thread_ts): replies under one thread.\n"
    "- slack_permalink(channel, ts): the permalink for one message."
)


def _client() -> MessagingBackend:
    for name, overlay in get_all_overlays().items():
        if Service.SLACK in overlay.config.required_third_party_services:
            messaging = messaging_from_overlay(name)
            if messaging is not None:
                return messaging
    msg = "No registered overlay declares a configured Slack messaging backend"
    raise RuntimeError(msg)


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


def register(server: FastMCP) -> None:
    server.add_tool(_slack_mentions, name="slack_mentions", annotations=_READ_ONLY)
    server.add_tool(_slack_channel_history, name="slack_channel_history", annotations=_READ_ONLY)
    server.add_tool(_slack_thread_replies, name="slack_thread_replies", annotations=_READ_ONLY)
    server.add_tool(_slack_permalink, name="slack_permalink", annotations=_READ_ONLY)
