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
from functools import partial
from typing import cast

from teatree.backends.slack.bot_errors import SlackReadRefusedError
from teatree.backends.slack.pagination import next_cursor
from teatree.backends.slack.self_identity import OwnSlackIdentity, is_self_authored, is_thread_root
from teatree.backends.slack.web_reads import Getter as TokenGetter
from teatree.types import RawAPIDict

logger = logging.getLogger(__name__)

type Getter = Callable[[str, dict[str, str | int]], RawAPIDict]

# 40 pages * 200 replies bounds a runaway walk at ~8000 replies; a hit is logged,
# never silently truncated, so a dedup caller can trust a full read below the cap.
_MAX_THREAD_PAGES = 40

# A `since`-bounded DM poll is walked whole so a burst larger than one page is
# not dropped; 20 pages * 20 messages bounds it at ~400 messages.
_MAX_DM_PAGES = 20
_DM_PAGE_SIZE = 20

# The refusals a bot gets for a channel it was never invited to, which the user's xoxp may still read.
_BOT_NOT_A_MEMBER = frozenset({"not_in_channel", "channel_not_found"})


def _messages(data: RawAPIDict) -> list[RawAPIDict]:
    if not data.get("ok"):
        return []
    messages = data.get("messages")
    return [cast("RawAPIDict", m) for m in messages if isinstance(m, dict)] if isinstance(messages, list) else []


def _walk_pages(
    get: Getter,
    method: str,
    params: dict[str, str | int],
    *,
    cap: int,
    subject: str,
) -> list[RawAPIDict]:
    """Cursor-follow *method* across every page, bounded by *cap*.

    The one Slack read walk in this module: a caller whose dedup or delivery
    verification depends on seeing every message needs the whole read, and a
    cap hit is logged rather than passing for a complete one. A refused page
    raises :class:`SlackReadRefusedError` and keeps nothing: what is collected
    is the NEWEST history, so keeping it would advance the caller's cursor past
    the page it never read.
    """
    collected: list[RawAPIDict] = []
    cursor: str | None = None
    for _ in range(cap):
        page = dict(params)
        if cursor:
            page["cursor"] = cursor
        data = get(method, page)
        if not data.get("ok"):
            raise SlackReadRefusedError(method, subject, str(data.get("error") or "unknown_error"))
        collected.extend(_messages(data))
        cursor = next_cursor(data)
        if cursor is None:
            return collected
    logger.warning("Slack %s hit the %d-page cap on %s; the read is truncated", method, cap, subject)
    return collected


def _read_or_abandon(walk: Callable[[], list[RawAPIDict]]) -> list[RawAPIDict]:
    """Run *walk* for the DM poll, where one refused read must not stall the loop."""
    try:
        return walk()
    except SlackReadRefusedError as refused:
        logger.warning("%s; the read is abandoned", refused)
        return []


def _walk_thread(get: Getter, channel: str, thread_ts: str) -> list[RawAPIDict]:
    params: dict[str, str | int] = {"channel": channel, "ts": thread_ts, "limit": 200}
    return _walk_pages(
        get,
        "conversations.replies",
        params,
        cap=_MAX_THREAD_PAGES,
        subject=f"thread {thread_ts} in {channel}",
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


def read_user_dms(
    *,
    get: Getter,
    channel: str,
    since: str,
    identity: OwnSlackIdentity | None,
) -> list[RawAPIDict]:
    """Return new DMs FROM the user, with thread replies, bot's own posts dropped.

    A ``since``-bounded poll is walked across every page of the window, so a
    burst larger than one page is not silently dropped. An unbounded poll reads
    the newest page only — Slack pages newest-first, so walking one would replay
    the entire DM history into the inbound pipeline.
    """
    params: dict[str, str | int] = {"channel": channel, "limit": _DM_PAGE_SIZE}
    if since:
        params["oldest"] = since
        # Inclusive of the anchor: `oldest` is exclusive by default, and the thread
        # fan-out below only reaches roots the history returns — so excluding the
        # cursor message would drop replies landing under the newest known root.
        params["inclusive"] = "true"
        history = _read_or_abandon(
            partial(
                _walk_pages, get, "conversations.history", params, cap=_MAX_DM_PAGES, subject=f"DM channel {channel}"
            )
        )
    else:
        history = _messages(get("conversations.history", params))
    result: list[RawAPIDict] = []
    for msg in history:
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
    for reply in _read_or_abandon(partial(_walk_thread, get, channel, thread_ts)):
        if reply.get("ts") == thread_ts or (identity is not None and is_self_authored(reply, identity)):
            continue
        reply.setdefault("channel", channel)
        replies.append(reply)
    return replies


__all__ = ["read_single_message", "read_thread_replies", "read_user_dms"]
