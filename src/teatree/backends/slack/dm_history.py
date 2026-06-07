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

from collections.abc import Callable
from typing import cast

from teatree.backends.slack.self_identity import OwnSlackIdentity, is_self_authored, is_thread_root
from teatree.types import RawAPIDict

type Getter = Callable[[str, dict[str, str | int]], RawAPIDict]


def _messages(data: RawAPIDict) -> list[RawAPIDict]:
    if not data.get("ok"):
        return []
    messages = data.get("messages")
    return [cast("RawAPIDict", m) for m in messages if isinstance(m, dict)] if isinstance(messages, list) else []


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
    for reply in _messages(get("conversations.replies", {"channel": channel, "ts": thread_ts, "limit": 50})):
        if reply.get("ts") == thread_ts or (identity is not None and is_self_authored(reply, identity)):
            continue
        reply.setdefault("channel", channel)
        replies.append(reply)
    return replies


__all__ = ["read_user_dms"]
