"""Slack Web API read+parse helpers, split out of ``SlackBotBackend``.

The channel-history poll, user-id lookup, and message-reactions read — each a
``conversations.*`` / ``users.*`` / ``reactions.get`` call plus its response
parsing — factored into free functions taking the backend's ``get`` callable
(the same seam ``dm_history`` uses) so ``bot.py`` stays under the module-health
LOC cap. The token-selection policy stays on the backend: it resolves the token
and passes it in, keeping these functions free of the Connect-membership concern.
"""

from typing import Protocol, cast

from teatree.backends.slack.bot_errors import GLOBAL_TOKEN_FAILURES
from teatree.backends.slack.pagination import next_cursor
from teatree.types import ChannelReadRefusedError, RawAPIDict, ScannerError

# Bounds the members walk at ~10k users so a lookup of a genuinely-absent handle
# terminates rather than paging an entire enterprise workspace.
_MAX_USER_PAGES = 50


class Getter(Protocol):
    def __call__(self, method: str, params: dict[str, str | int], *, token: str = "") -> RawAPIDict: ...


def read_channel_history_or_refuse(*, get: Getter, channel: str, token: str, limit: int = 50) -> list[RawAPIDict]:
    """Like :func:`read_channel_history` but RAISES on a channel-scoped refusal.

    The seam for interactive, single-channel callers (the MCP Slack group). The
    scanner keeps the swallowing variant; nobody has to choose between a resilient
    poll loop and an honest answer.
    """
    return _read_channel_history(get=get, channel=channel, token=token, limit=limit, refuse=True)


def read_channel_history(*, get: Getter, channel: str, token: str, limit: int = 50) -> list[RawAPIDict]:
    """Return the most recent *limit* messages in *channel*, ``channel`` stamped (#1255).

    Used by :class:`SlackBroadcastsScanner` to poll review-broadcast channels for
    MR URLs. This is a "read taken as the post" — the scanner will later react on
    these messages — so the caller passes the WRITE-op token: on a Slack-Connect
    channel the bot token is rejected for *both* history reads and reactions with
    ``mcp_externally_shared_channel_restricted``, so a bot-token read would return
    empty and silently drop every broadcast; the WRITE token keeps read-token ==
    post-token (#1084). A global token failure (auth / missing scope / rate limit /
    deactivated) raises :class:`ScannerError` so the dispatcher records it and DMs
    the user (#1287); a channel-scoped failure returns ``[]`` so one slow channel
    never breaks the scan loop (#1255). ``channel`` is stamped on each message so
    downstream consumers don't have to thread it back in. An interactive caller that
    must distinguish "empty" from "unreadable" uses
    :func:`read_channel_history_or_refuse` instead.
    """
    return _read_channel_history(get=get, channel=channel, token=token, limit=limit, refuse=False)


def _read_channel_history(
    *,
    get: Getter,
    channel: str,
    token: str,
    limit: int,
    refuse: bool,
) -> list[RawAPIDict]:
    data = get(
        "conversations.history",
        {"channel": channel, "limit": max(1, min(limit, 200))},
        token=token,
    )
    if not data.get("ok"):
        error_code = str(data.get("error", ""))
        if error_code in GLOBAL_TOKEN_FAILURES:
            raise ScannerError(
                scanner="slack_broadcasts",
                error_class=GLOBAL_TOKEN_FAILURES[error_code],
                detail=f"conversations.history on {channel}: {error_code}",
            )
        if refuse:
            raise ChannelReadRefusedError(channel, error_code or "unknown_error")
        return []
    messages = data.get("messages")
    if not isinstance(messages, list):
        return []
    out: list[RawAPIDict] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        entry = cast("RawAPIDict", msg)
        entry.setdefault("channel", channel)
        out.append(entry)
    return out


def read_reactions(*, get: Getter, channel: str, ts: str, token: str) -> list[str]:
    """Return the emoji names currently set on a message."""
    data = get(
        "reactions.get",
        {"channel": channel, "timestamp": ts},
        token=token,
    )
    if not data.get("ok"):
        return []
    message = cast("RawAPIDict", data.get("message") or {})
    reactions = message.get("reactions")
    if not isinstance(reactions, list):
        return []
    names: list[str] = []
    for raw_reaction in reactions:
        if not isinstance(raw_reaction, dict):
            continue
        reaction = cast("RawAPIDict", raw_reaction)
        name = reaction.get("name")
        if isinstance(name, str):
            names.append(name)
    return names


def _match_member_id(members: object, name: str) -> str:
    """The id of the first member whose ``name``/``real_name`` equals *name*, else ``""``."""
    if not isinstance(members, list):
        return ""
    for raw_member in members:
        if not isinstance(raw_member, dict):
            continue
        member = cast("RawAPIDict", raw_member)
        if member.get("name") == name or member.get("real_name") == name:
            user_id = member.get("id")
            if isinstance(user_id, str):
                return user_id
    return ""


def resolve_user_id(*, get: Getter, handle: str) -> str:
    """Look up a Slack user id from a handle (``@alice`` or ``alice``)."""
    clean = handle.lstrip("@")
    if not clean:
        return ""
    data = get("users.lookupByEmail", {"email": clean}) if "@" in clean else {}
    if data.get("ok"):
        user = cast("RawAPIDict", data.get("user") or {})
        user_id = user.get("id")
        if isinstance(user_id, str):
            return user_id
    # Fallback: list users and match by name, cursor-following every page so a
    # handle past the first page is not reported "not found". The loop scanners
    # cache the result via ``functools.lru_cache`` upstream.
    cursor: str | None = None
    for _ in range(_MAX_USER_PAGES):
        params: dict[str, str | int] = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        listing = get("users.list", params)
        matched = _match_member_id(listing.get("members"), clean)
        if matched:
            return matched
        cursor = next_cursor(listing)
        if cursor is None:
            return ""
    return ""
