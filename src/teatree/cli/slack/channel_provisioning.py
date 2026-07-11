"""Join the per-overlay bot to its review-broadcast channels (#1686 scope).

The review-request and broadcast scanners post into the overlay's
review-broadcast channels (``OverlayBase.get_review_broadcast_channels``). A
freshly-installed bot is not a member of those channels, so its first post or
reaction there fails ``not_in_channel`` — the canary the user reported. This
module joins the bot to each configured channel at setup time.

``conversations.join`` lets the bot add *itself* to a public channel using its
own bot token (needs ``channels:read`` / ``chat:write.public``). Private and
Slack-Connect channels reject self-join; for those the caller surfaces a clean
"invite the bot manually" line rather than failing the whole provision.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from teatree.backends.slack.bot import SlackBotBackend


class JoinStatus(Enum):
    JOINED = "joined"
    ALREADY_IN = "already_in"
    NEEDS_MANUAL_INVITE = "needs_manual_invite"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ChannelJoinResult:
    """Outcome of joining the bot to one review-broadcast channel."""

    status: JoinStatus
    channel_name: str
    channel_id: str
    detail: str = ""


def join_channel(*, backend: SlackBotBackend, channel_id: str, channel_name: str = "") -> ChannelJoinResult:
    """Join the bot to *channel_id* via ``conversations.join`` (idempotent).

    Slack returns ``ok:true`` both on a fresh join and when the bot is already
    a member, so the call is naturally idempotent. ``already_in_channel`` and
    a missing-scope/Connect rejection are mapped to actionable statuses; any
    other ``ok:false`` is reported as ``FAILED`` with the raw error.
    """
    data = backend.join_conversation(channel_id)
    if data.get("ok"):
        already = bool(data.get("already_in_channel"))
        return ChannelJoinResult(
            status=JoinStatus.ALREADY_IN if already else JoinStatus.JOINED,
            channel_name=channel_name,
            channel_id=channel_id,
        )
    error = str(data.get("error", "unknown_error"))
    if error in {"method_not_supported_for_channel_type", "is_archived", "restricted_action", "missing_scope"}:
        return ChannelJoinResult(
            status=JoinStatus.NEEDS_MANUAL_INVITE,
            channel_name=channel_name,
            channel_id=channel_id,
            detail=error,
        )
    return ChannelJoinResult(
        status=JoinStatus.FAILED,
        channel_name=channel_name,
        channel_id=channel_id,
        detail=error,
    )


def join_review_channels(
    *,
    backend: SlackBotBackend,
    channels: list[tuple[str, str]],
) -> list[ChannelJoinResult]:
    """Join the bot to every ``(name, id)`` review-broadcast channel.

    Channels with an empty id are skipped. The list comes from
    ``OverlayBase.get_review_broadcast_channels`` so this stays overlay-agnostic.
    """
    results: list[ChannelJoinResult] = []
    for name, channel_id in channels:
        if not channel_id:
            continue
        results.append(join_channel(backend=backend, channel_id=channel_id, channel_name=name))
    return results


def render_join_result(result: ChannelJoinResult, echo: Callable[[str], None]) -> None:
    """Emit one human-readable line per channel-join outcome."""
    label = f"`{result.channel_name}` ({result.channel_id})" if result.channel_name else result.channel_id
    if result.status is JoinStatus.JOINED:
        echo(f"OK    Bot joined review channel {label}.")
    elif result.status is JoinStatus.ALREADY_IN:
        echo(f"OK    Bot already in review channel {label}.")
    elif result.status is JoinStatus.NEEDS_MANUAL_INVITE:
        echo(f"ACTION  Invite the bot to {label} manually (`/invite @<bot>`) — {result.detail}.")
    else:
        echo(f"WARN  Could not join review channel {label}: {result.detail}.")


__all__ = [
    "ChannelJoinResult",
    "JoinStatus",
    "join_channel",
    "join_review_channels",
    "render_join_result",
]
