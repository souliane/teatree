"""Slack message-history reads: one message, one thread, and the cursor-walking page reader.

Split out of ``SlackBotBackend`` so the backend stays under the module-health LOC
cap. The Slack ``conversations.*`` reads do not stamp the ``channel`` field on each
message — it is the request parameter, not part of the response — so it is stamped
here for downstream consumers (#1043).
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import cast

from teatree.backends.slack.bot_errors import SlackReadRefusedError
from teatree.backends.slack.pagination import next_cursor
from teatree.backends.slack.web_reads import Getter as TokenGetter
from teatree.types import RawAPIDict

logger = logging.getLogger(__name__)

type Getter = Callable[[str, dict[str, str | int]], RawAPIDict]


@dataclass(frozen=True, slots=True)
class PageLimit:
    cap: int
    subject: str
    items_key: str = "messages"


# 40 pages * 200 replies bounds a runaway walk at ~8000 replies; a hit is logged,
# never silently truncated, so a dedup caller can trust a full read below the cap.
_MAX_THREAD_PAGES = 40

# The refusals a bot gets for a channel it was never invited to, which the user's xoxp may still read.
_BOT_NOT_A_MEMBER = frozenset({"not_in_channel", "channel_not_found"})


def _messages(data: RawAPIDict, key: str = "messages") -> list[RawAPIDict]:
    if not data.get("ok"):
        return []
    messages = data.get(key)
    return [cast("RawAPIDict", m) for m in messages if isinstance(m, dict)] if isinstance(messages, list) else []


def walk_pages(
    get: Getter,
    method: str,
    params: dict[str, str | int],
    *,
    limit: PageLimit,
    strict: bool = False,
) -> list[RawAPIDict]:
    """Cursor-follow *method* across every page, bounded by *cap*.

    The one Slack read walk: a caller whose dedup or delivery
    verification depends on seeing every message needs the whole read, and a
    cap hit is logged rather than passing for a complete one. A refused page
    raises :class:`SlackReadRefusedError` and keeps nothing: what is collected
    is the NEWEST history, so keeping it would advance the caller's cursor past
    the page it never read.
    """
    collected: list[RawAPIDict] = []
    cursor: str | None = None
    for _ in range(limit.cap):
        page = dict(params)
        if cursor:
            page["cursor"] = cursor
        data = get(method, page)
        if not data.get("ok"):
            raise SlackReadRefusedError(method, limit.subject, str(data.get("error") or "unknown_error"))
        collected.extend(_messages(data, limit.items_key))
        cursor = next_cursor(data)
        if cursor is None:
            return collected
    if strict:
        raise SlackReadRefusedError(method, limit.subject, "page_cap_reached")
    logger.warning("Slack %s hit the %d-page cap on %s; the read is truncated", method, limit.cap, limit.subject)
    return collected


def _walk_thread(get: Getter, channel: str, thread_ts: str) -> list[RawAPIDict]:
    params: dict[str, str | int] = {"channel": channel, "ts": thread_ts, "limit": 200}
    return walk_pages(
        get,
        "conversations.replies",
        params,
        limit=PageLimit(_MAX_THREAD_PAGES, f"thread {thread_ts} in {channel}"),
    )


def read_single_message(*, get: Getter, channel: str, ts: str) -> RawAPIDict:
    """Fetch one message by ``(channel, ts)`` via ``conversations.history``.

    Returns the message dict (with ``channel`` stamped on) or ``{}`` on any
    non-ok response / no match. The caller applies its own self-message
    transforms; this is the raw read.
    """
    if not channel or not ts:
        return {}
    params: dict[str, str | int] = {"channel": channel, "latest": ts, "inclusive": "true", "limit": 1}
    messages = _messages(get("conversations.history", params))
    if not messages:
        return {}
    first = messages[0]
    first.setdefault("channel", channel)
    return first


def read_thread_replies(*, get: TokenGetter, channel: str, thread_ts: str, user_token: str = "") -> list[RawAPIDict]:
    """Return every message in the thread rooted at ``thread_ts`` (#2061), ``channel`` stamped.

    The canonical thread-root read used by the answer pipeline's pre-post
    dedup and post-delivery verification: a reply re-parents to the root, so
    a read-back keyed on a non-root user-message ts misses it. A read Slack
    refuses raises :class:`SlackReadRefusedError` — ``[]`` only ever means an
    empty thread — and each caller decides what a refusal means to it. A
    channel the bot was never invited to is re-read through *user_token*.
    """
    if not channel or not thread_ts:
        return []
    try:
        replies = _walk_thread(get, channel, thread_ts)
    except SlackReadRefusedError as refused:
        if not user_token or refused.error_code not in _BOT_NOT_A_MEMBER:
            raise
        replies = _walk_thread(partial(get, token=user_token), channel, thread_ts)
    for reply in replies:
        reply.setdefault("channel", channel)
    return replies


__all__ = ["PageLimit", "read_single_message", "read_thread_replies", "walk_pages"]
