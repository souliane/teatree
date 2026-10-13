"""The owner's bot DM: history, replies under thread roots active in the window, owner reactions."""

from typing import cast

from teatree.backends.slack.bot import SlackBotBackend
from teatree.backends.slack.dm_history import PageLimit, walk_pages
from teatree.backends.slack.self_identity import OwnSlackIdentity, is_self_authored, is_thread_root
from teatree.slack_catchup.window import CatchUpRefusedError, CatchUpWindow, ts_seconds
from teatree.types import RawAPIDict

#: 20 pages of 200 against a measured worst week-plus-a-day of ~1,840 DM posts.
_HISTORY_PAGE_CAP = 20
_REPLY_PAGE_CAP = 40
_PAGE_SIZE = 200


def owner_reactions(message: RawAPIDict, *, owner: str, channel: str) -> list[RawAPIDict]:
    """Each reaction the owner holds on *message*, as the ``reaction_added`` event Slack would have sent."""
    reactions = message.get("reactions")
    if not isinstance(reactions, list) or not owner:
        return []
    events: list[RawAPIDict] = []
    for raw in reactions:
        reaction = cast("RawAPIDict", raw) if isinstance(raw, dict) else {}
        users = reaction.get("users")
        name = reaction.get("name")
        if isinstance(name, str) and isinstance(users, list) and owner in users:
            events.append(
                {
                    "type": "reaction_added",
                    "user": owner,
                    "reaction": name,
                    "item": {"type": "message", "channel": channel, "ts": message.get("ts")},
                }
            )
    return events


def _message_events(
    message: RawAPIDict, *, channel: str, owner: str, window: CatchUpWindow, identity: OwnSlackIdentity
) -> list[RawAPIDict]:
    events: list[RawAPIDict] = []
    if window.includes(message.get("ts")) and not is_self_authored(message, identity):
        events.append({**message, "type": "message", "channel_type": "im", "channel": channel})
    events.extend(owner_reactions(message, owner=owner, channel=channel))
    return events


def owner_dm_events(backend: SlackBotBackend, *, window: CatchUpWindow, identity: OwnSlackIdentity) -> list[RawAPIDict]:
    """Every DM event the window holds, oldest first; a refused page raises and nothing is returned."""
    owner = backend.user_id
    channel = backend.open_dm(owner) if owner else ""
    if not channel:
        msg = f"no bot DM channel resolves for the owner {owner!r}"
        raise CatchUpRefusedError(msg)
    get = backend.get
    history = walk_pages(
        get,
        "conversations.history",
        {
            "channel": channel,
            "oldest": window.oldest_ts,
            "latest": f"{window.end:.6f}",
            "inclusive": "true",
            "limit": _PAGE_SIZE,
        },
        limit=PageLimit(_HISTORY_PAGE_CAP, f"DM channel {channel}"),
        strict=True,
    )
    events: list[RawAPIDict] = []
    for message in sorted(history, key=lambda m: ts_seconds(m.get("ts"))):
        events.extend(_message_events(message, channel=channel, owner=owner, window=window, identity=identity))
        if not is_thread_root(message) or ts_seconds(message.get("latest_reply")) < window.start:
            continue
        root = str(message["ts"])
        replies = walk_pages(
            get,
            "conversations.replies",
            {
                "channel": channel,
                "ts": root,
                "oldest": window.start_ts,
                "latest": f"{window.end:.6f}",
                "limit": _PAGE_SIZE,
            },
            limit=PageLimit(_REPLY_PAGE_CAP, f"thread {root} in {channel}"),
            strict=True,
        )
        for reply in sorted(replies, key=lambda m: ts_seconds(m.get("ts"))):
            if reply.get("ts") != root:
                events.extend(_message_events(reply, channel=channel, owner=owner, window=window, identity=identity))

    def event_time(event: RawAPIDict) -> float:
        item = event.get("item")
        stamp = item.get("ts") if isinstance(item, dict) else event.get("ts")
        return ts_seconds(stamp)

    return sorted(events, key=event_time)
