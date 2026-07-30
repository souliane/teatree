"""Slack DM-history reading: poll, self-filter, thread fan-out (#1044/#1346).

The inbound DM-read concern, split out of ``SlackBotBackend`` so the backend
stays under the module-health LOC cap. :func:`read_user_dms` polls
``conversations.history`` on the bot's DM channel, drops the bot's own
top-level posts (the #1346 self-filter, via
:func:`~teatree.backends.slack.self_identity.is_self_authored`), and fans out
to ``conversations.replies`` for every thread root so replies are picked up.

The Slack ``conversations.*`` reads do not stamp the ``channel`` field on each
message — it is the request parameter, not part of the response — so it is
stamped here for downstream consumers (#1043).
"""

import logging
from collections.abc import Callable
from typing import cast

from teatree.backends.slack.pagination import next_cursor
from teatree.backends.slack.self_identity import OwnSlackIdentity, is_self_authored, is_thread_root
from teatree.types import RawAPIDict

logger = logging.getLogger(__name__)

type Getter = Callable[[str, dict[str, str | int]], RawAPIDict]

# 40 pages * 200 replies bounds a runaway walk at ~8000 replies; a hit is logged,
# never silently truncated, so a dedup caller can trust a full read below the cap.
_MAX_THREAD_PAGES = 40


def _messages(data: RawAPIDict) -> list[RawAPIDict]:
    if not data.get("ok"):
        return []
    messages = data.get("messages")
    return [cast("RawAPIDict", m) for m in messages if isinstance(m, dict)] if isinstance(messages, list) else []


def _walk_thread(get: Getter, channel: str, thread_ts: str) -> list[RawAPIDict]:
    """Cursor-follow ``conversations.replies`` across every page of the thread.

    Mirrors the ``conversations.history`` cursor walk in ``client.py`` so a
    thread with more than one page of replies is read whole — the pre-post dedup
    and post-delivery verification that read it depend on seeing every reply.
    """
    collected: list[RawAPIDict] = []
    cursor: str | None = None
    for _ in range(_MAX_THREAD_PAGES):
        params: dict[str, str | int] = {"channel": channel, "ts": thread_ts, "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = get("conversations.replies", params)
        collected.extend(_messages(data))
        cursor = next_cursor(data)
        if cursor is None:
            return collected
    logger.warning(
        "Slack conversations.replies hit the %d-page cap on thread %s; the reply read is truncated",
        _MAX_THREAD_PAGES,
        thread_ts,
    )
    return collected


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


def read_thread_replies(*, get: Getter, channel: str, thread_ts: str) -> list[RawAPIDict]:
    """Return every message in the thread rooted at ``thread_ts`` (#2061).

    The canonical thread-root read used by the answer pipeline's pre-post
    dedup and post-delivery verification: a reply re-parents to the root, so
    a read-back keyed on a non-root user-message ts misses it. ``channel`` is
    stamped on each message; ``[]`` on any non-ok response so a transient
    read failure is handled by the conservative-retry caller.
    """
    if not channel or not thread_ts:
        return []
    replies: list[RawAPIDict] = []
    for reply in _walk_thread(get, channel, thread_ts):
        reply.setdefault("channel", channel)
        replies.append(reply)
    return replies


def read_user_dms(
    *,
    get: Getter,
    channel: str,
    since: str,
    identity: OwnSlackIdentity | None,
) -> list[RawAPIDict]:
    """Return new DMs FROM the user, with thread replies, bot's own posts dropped."""
    params: dict[str, str | int] = {"channel": channel, "limit": 20}
    if since:
        params["oldest"] = since
    result: list[RawAPIDict] = []
    for msg in _messages(get("conversations.history", params)):
        msg.setdefault("channel", channel)
        if identity is None or not is_self_authored(msg, identity):
            result.append(msg)
        if is_thread_root(msg):
            result.extend(_thread_replies(get=get, channel=channel, thread_ts=str(msg["ts"]), identity=identity))
    return result


def _thread_replies(
    *,
    get: Getter,
    channel: str,
    thread_ts: str,
    identity: OwnSlackIdentity | None,
) -> list[RawAPIDict]:
    replies: list[RawAPIDict] = []
    for reply in _walk_thread(get, channel, thread_ts):
        if reply.get("ts") == thread_ts or (identity is not None and is_self_authored(reply, identity)):
            continue
        reply.setdefault("channel", channel)
        replies.append(reply)
    return replies


__all__ = ["read_single_message", "read_thread_replies", "read_user_dms"]
