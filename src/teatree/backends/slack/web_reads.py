"""Slack Web API read+parse helpers, split out of ``SlackBotBackend``.

The channel-history poll, user-id lookup, and message-reactions read — each a
``conversations.*`` / ``users.*`` / ``reactions.get`` call plus its response
parsing — factored into free functions taking the backend's ``get`` callable
(the same seam ``dm_history`` uses) so ``bot.py`` stays under the module-health
LOC cap. The backend selects the first token; a bot membership refusal can
retry with its configured user token. Connect routing stays on the backend.
"""

import re
from collections.abc import Callable
from typing import Protocol, TypeGuard, cast

from teatree.backends.slack.bot_errors import GLOBAL_TOKEN_FAILURES, SlackReadRefusedError
from teatree.backends.slack.pagination import next_cursor
from teatree.types import ChannelReadRefusedError, RawAPIDict, ScannerError

# Bounds the members walk at ~10k users so a lookup of a genuinely-absent handle
# terminates rather than paging an entire enterprise workspace.
_MAX_USER_PAGES = 50
_USERGROUP_ID = re.compile(r"S[A-Z0-9]{8,}")
SLACK_USER_ID_RE = re.compile(r"[UW][A-Z0-9]{6,}")


def is_slack_user_id(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and SLACK_USER_ID_RE.fullmatch(value) is not None


# A bot can be absent from a channel that the configured user can read.
_BOT_NOT_A_MEMBER = frozenset({"not_in_channel", "channel_not_found"})


class Getter(Protocol):
    def __call__(self, method: str, params: dict[str, str | int], *, token: str = "") -> RawAPIDict: ...


def read_conversations(*, get: Getter, token: str, cursor: str = "", limit: int = 100) -> RawAPIDict:
    max_page_size = 200
    if not 1 <= limit <= max_page_size:
        msg = f"limit must be between 1 and {max_page_size}"
        raise ValueError(msg)
    params: dict[str, str | int] = {"types": "public_channel,private_channel,mpim,im", "limit": limit}
    if cursor:
        params["cursor"] = cursor
    data = get("conversations.list", params, token=token)
    channels = data.get("channels")
    if data.get("ok") is not True or not isinstance(channels, list):
        msg = f"Slack conversations.list failed: {data.get('error') or 'malformed response'}"
        raise ValueError(msg)
    metadata = data.get("response_metadata")
    next_page = metadata.get("next_cursor", "") if isinstance(metadata, dict) else ""
    return {"channels": channels, "next_cursor": next_page}


class SlackUserDirectoryUnreadableError(RuntimeError):
    pass


def read_with_user_token_fallback(
    read: Callable[[str], list[RawAPIDict]], *, token: str, user_token: str
) -> list[RawAPIDict]:
    try:
        return read(token)
    except (SlackReadRefusedError, ChannelReadRefusedError) as refused:
        # Thread and history readers use distinct exceptions for the same Slack code.
        if not user_token or token == user_token or refused.error_code not in _BOT_NOT_A_MEMBER:
            raise
        return read(user_token)


def read_channel_history_or_refuse(
    *, get: Getter, channel: str, token: str, user_token: str = "", limit: int = 50
) -> list[RawAPIDict]:
    """Read one channel, retrying a bot membership refusal as the user when possible."""
    return read_with_user_token_fallback(
        lambda auth: _read_channel_history(get=get, channel=channel, token=auth, limit=limit, refuse=True),
        token=token,
        user_token=user_token,
    )


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


def resolve_unique_active_user_id(*, get: Getter, handle: str) -> str:
    clean = handle.strip().lstrip("@").casefold()
    if not clean:
        return ""
    matches: set[str] = set()
    cursor: str | None = None
    for _ in range(_MAX_USER_PAGES):
        params: dict[str, str | int] = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        listing = get("users.list", params)
        members = listing.get("members")
        if listing.get("ok") is not True or not isinstance(members, list):
            msg = "Slack user directory could not be read"
            raise SlackUserDirectoryUnreadableError(msg)
        for raw_member in members:
            if isinstance(raw_member, dict) and (user_id := _active_directory_match(raw_member, clean)):
                matches.add(user_id)
        cursor = next_cursor(listing)
        if cursor is None:
            return next(iter(matches)) if len(matches) == 1 else ""
    return ""


def _active_directory_match(member: RawAPIDict, clean: str) -> str:
    if (
        member.get("deleted") is not False
        or any(member.get(flag) is not False for flag in ("is_bot", "is_restricted", "is_ultra_restricted"))
        or member.get("is_app_user") is True
    ):
        return ""
    profile = member.get("profile")
    names = (member.get("name"), member.get("real_name"))
    if isinstance(profile, dict):
        names += (profile.get("display_name"),)
    user_id = member.get("id")
    if not any(isinstance(name, str) and name.casefold() == clean for name in names) and not (
        is_slack_user_id(user_id) and user_id.casefold() == clean
    ):
        return ""
    return user_id if is_slack_user_id(user_id) else ""


def resolve_usergroup_id(*, get: Getter, handle: str) -> str:
    """Look up a Slack user-group id from its handle (``@team`` or ``team``); an id passes through."""
    clean = handle.lstrip("@")
    if _USERGROUP_ID.fullmatch(clean):
        return clean
    if not clean:
        return ""
    listing = get("usergroups.list", {})
    groups = listing.get("usergroups") if listing.get("ok") else None
    if not isinstance(groups, list):
        return ""
    for raw_group in groups:
        if not isinstance(raw_group, dict):
            continue
        group = cast("RawAPIDict", raw_group)
        group_id = group.get("id")
        if group.get("handle") == clean and isinstance(group_id, str):
            return group_id
    return ""
