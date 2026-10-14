"""The catch-up pass: compute the window, read it whole, then hand every item to *deliver*."""

import datetime as dt
import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import partial
from typing import cast

from teatree.backends.slack.bot import SlackBotBackend
from teatree.backends.slack.bot_errors import SlackReadRefusedError
from teatree.backends.slack.dm_history import PageLimit, walk_pages
from teatree.backends.slack.self_identity import resolve_own_identity
from teatree.slack_catchup.dm import owner_dm_events, owner_reactions
from teatree.slack_catchup.window import SLICE_SECONDS, CatchUpRefusedError, CatchUpWindow, ts_seconds
from teatree.types import RawAPIDict

type Deliver = Callable[[RawAPIDict], bool]
logger = logging.getLogger(__name__)
_PAGE_CAP = 20
_SEARCH_PAGE_CAP = 100


def _member_channels(backend: SlackBotBackend) -> set[str]:
    get = partial(backend.get, token=backend.user_token)
    channels = walk_pages(
        get,
        "users.conversations",
        {"types": "public_channel,private_channel,mpim", "limit": 200},
        limit=PageLimit(_PAGE_CAP, "owner channel membership", items_key="channels"),
        strict=True,
    )
    return {str(channel["id"]) for channel in channels if channel.get("id") and channel.get("is_member") is not False}


def _searched_mentions(backend: SlackBotBackend, window: CatchUpWindow, members: set[str]) -> list[RawAPIDict]:
    since = dt.datetime.fromtimestamp(window.oldest, tz=dt.UTC).date()
    before = dt.datetime.fromtimestamp(window.end, tz=dt.UTC).date() + dt.timedelta(days=1)
    query = f"<@{backend.user_id}> after:{since - dt.timedelta(days=1)} before:{before}"
    found: list[RawAPIDict] = []
    for page in range(1, _SEARCH_PAGE_CAP + 1):
        result = backend.get(
            "search.messages",
            {"query": query, "count": 100, "page": page, "sort": "timestamp", "sort_dir": "asc"},
            token=backend.user_token,
        )
        if not result.get("ok"):
            method = "search.messages"
            raise SlackReadRefusedError(method, query, str(result.get("error") or "unknown_error"))
        messages = result.get("messages")
        if not isinstance(messages, dict):
            message = "search.messages returned no messages envelope"
            raise CatchUpRefusedError(message)
        matches = messages.get("matches")
        paging = messages.get("paging")
        if not isinstance(matches, list) or not isinstance(paging, dict) or not isinstance(paging.get("pages"), int):
            message = "search.messages returned invalid paging"
            raise CatchUpRefusedError(message)
        for raw in matches:
            if not isinstance(raw, dict):
                continue
            match = cast("RawAPIDict", raw)
            channel = match.get("channel")
            channel_id = channel.get("id") if isinstance(channel, dict) else None
            if isinstance(channel_id, str) and channel_id in members and window.includes(match.get("ts")):
                found.append({**match, "type": "app_mention", "channel": channel_id})
        if page >= paging["pages"]:
            return found
    message = "search.messages page cap reached"
    raise CatchUpRefusedError(message)


def _owner_channel_reactions(backend: SlackBotBackend, window: CatchUpWindow, members: set[str]) -> list[RawAPIDict]:
    get = partial(backend.get, token=backend.user_token)
    items = walk_pages(
        get,
        "reactions.list",
        {"user": backend.user_id, "full": "true", "limit": 100},
        limit=PageLimit(_PAGE_CAP, "owner reactions", items_key="items"),
        strict=True,
    )
    events: list[RawAPIDict] = []
    for item in items:
        channel = item.get("channel")
        channel_id = channel.get("id") if isinstance(channel, dict) else channel
        message = item.get("message")
        if item.get("type") != "message" or not isinstance(channel_id, str) or channel_id not in members:
            continue
        if isinstance(message, dict) and window.oldest <= ts_seconds(message.get("ts")) < window.end:
            events.extend(owner_reactions(cast("RawAPIDict", message), owner=backend.user_id, channel=channel_id))
    return events


def _non_dm_events(backend: SlackBotBackend, window: CatchUpWindow) -> list[RawAPIDict]:
    members = _member_channels(backend)
    return [*_searched_mentions(backend, window, members), *_owner_channel_reactions(backend, window, members)]


def _event_time(event: RawAPIDict) -> float:
    item = event.get("item")
    return ts_seconds(item.get("ts") if isinstance(item, dict) else event.get("ts"))


@dataclass(frozen=True, slots=True)
class CatchUpSlice:
    caught_up_to: float
    oldest: float
    delivered: int


def catch_up(backend: SlackBotBackend, *, alive_at: float | None, deliver: Deliver) -> Iterator[CatchUpSlice]:
    """Deliver every item the window holds; raise, delivering nothing, when any read is refused.

    *alive_at* is when the listener was last known alive; ``None`` means no evidence,
    which walks the whole capped window.
    """
    window = CatchUpWindow.since(alive_at, now=time.time())
    identity = resolve_own_identity(backend)
    if identity is None:
        msg = "the bot's own Slack identity is unresolved"
        raise CatchUpRefusedError(msg)
    if not backend.user_token:
        logger.warning("Slack catch-up cannot recover channel mentions or reactions without a user token")
    start = window.start
    while start < window.end:
        end = min(start + SLICE_SECONDS, window.end)
        slice_window = CatchUpWindow(start=start, oldest=start - (window.start - window.oldest), end=end)
        events = [*owner_dm_events(backend, window=slice_window, identity=identity)]
        if backend.user_token:
            events.extend(_non_dm_events(backend, slice_window))
        events.sort(key=_event_time)
        delivered = sum(1 for event in events if deliver(event))
        yield CatchUpSlice(caught_up_to=end, oldest=slice_window.oldest, delivered=delivered)
        start = end
