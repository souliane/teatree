"""``SlackBotBackend`` — Slack messaging via the Web API.

Implements :class:`teatree.backends.protocols.MessagingBackend`. Outbound posts,
reactions, and user-id resolution use the Web API directly via httpx; inbound
``fetch_mentions`` / ``fetch_dms`` stream live events through Socket Mode and
are wired up by the ``t3 setup slack-bot`` walkthrough (see BLUEPRINT § 3.6).

The Phase 3 surface is intentionally minimal: enough for outbound routing
from the loop's scanners, with the Socket Mode receiver delivering inbound
events into the same backend through a queue managed by Phase 3.6.

Slack-Connect externally-shared channels reject reactions posted with a
bot token (``mcp_externally_shared_channel_restricted``).  When the human
user's OAuth token (``xoxp-…``) is configured via ``user_token_ref`` in
``~/.teatree.toml``, ``react`` and ``get_reactions`` authenticate with
that token instead.  Everything else (DMs, ``chat.postMessage``,
``conversations.open``, ``users.lookupByEmail``) keeps using the bot
token because those endpoints are scoped to the bot's own IM channels
and cache.
"""

from typing import cast

import httpx

from teatree.types import RawAPIDict

type SlackPayload = dict[str, object]


def _is_bot_authored(msg: RawAPIDict, bot_id: str) -> bool:
    return msg.get("user") == bot_id or msg.get("bot_id") == bot_id


def _is_thread_root(msg: RawAPIDict) -> bool:
    thread_ts = msg.get("thread_ts")
    return isinstance(thread_ts, str) and bool(thread_ts) and thread_ts == msg.get("ts")


class SlackBotBackend:
    """MessagingBackend backed by a Slack bot token, optionally with a user token.

    ``bot_token`` (``xoxb-…``) authorises Web API calls for DMs, posts, and
    bot-scoped lookups.
    ``app_token`` (``xapp-…``) authorises Socket Mode and is consumed by the
    Phase 3.6 walkthrough — kept on the instance so the bot starter can pick
    it up without a second config read.
    ``user_token`` (``xoxp-…``) authorises reactions in Slack-Connect
    externally-shared channels where the bot token is rejected by the workspace
    restriction policy.  When unset, reactions fall back to the bot token.
    ``user_id`` is the Slack user id of the human the bot speaks for; scanners
    use it to filter @mentions targeted at that user.
    """

    def __init__(
        self,
        *,
        bot_token: str = "",
        app_token: str = "",
        user_token: str = "",
        user_id: str = "",
    ) -> None:
        self._bot_token = bot_token
        self._app_token = app_token
        self._user_token = user_token
        self._user_id = user_id
        self._cached_bot_id: str | None = None
        # Inbound queues populated by the Phase 3.6 Socket Mode receiver. Each
        # tick the loop scanner drains them via ``fetch_mentions`` /
        # ``fetch_dms``; the receiver calls ``enqueue_mention`` / ``enqueue_dm``.
        self._mentions: list[RawAPIDict] = []
        self._dms: list[RawAPIDict] = []

    @property
    def app_token(self) -> str:
        return self._app_token

    @property
    def user_id(self) -> str:
        return self._user_id

    @property
    def user_token(self) -> str:
        return self._user_token

    def enqueue_mention(self, event: RawAPIDict) -> None:
        """Push a Socket Mode ``app_mention`` event into the inbound queue."""
        self._mentions.append(event)

    def enqueue_dm(self, event: RawAPIDict) -> None:
        """Push a Socket Mode ``message.im`` event into the inbound queue."""
        self._dms.append(event)

    def _post(self, method: str, payload: SlackPayload, *, token: str = "") -> RawAPIDict:
        auth = token or self._bot_token
        if not auth:
            return {}
        response = httpx.post(
            f"https://slack.com/api/{method}",
            headers={
                "Authorization": f"Bearer {auth}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json=payload,
            timeout=10.0,
        )
        response.raise_for_status()
        return cast("RawAPIDict", response.json())

    def _get(self, method: str, params: dict[str, str | int], *, token: str = "") -> RawAPIDict:
        auth = token or self._bot_token
        if not auth:
            return {}
        response = httpx.get(
            f"https://slack.com/api/{method}",
            headers={"Authorization": f"Bearer {auth}"},
            params=params,
            timeout=10.0,
        )
        response.raise_for_status()
        return cast("RawAPIDict", response.json())

    def _reaction_token(self) -> str:
        """Token authorising ``reactions.*`` calls.

        Slack-Connect externally-shared channels block bot tokens with
        ``mcp_externally_shared_channel_restricted``.  When the human's
        ``xoxp`` is configured we route reactions through it so they post
        from the user's identity; otherwise fall back to the bot token
        for the legacy single-credential case.
        """
        return self._user_token or self._bot_token

    def fetch_mentions(self, *, since: str = "") -> list[RawAPIDict]:
        """Drain queued Socket Mode mentions and return them in order.

        ``since`` is accepted for protocol compatibility but ignored — the
        Socket Mode receiver delivers events in real time, so the queue
        only ever holds events that arrived after the previous tick.
        """
        _ = since
        events, self._mentions = self._mentions, []
        return events

    def fetch_dms(self, *, since: str = "") -> list[RawAPIDict]:
        """Return new DMs from the user, including thread replies.

        Drains the Socket Mode queue first (populated by a running receiver).
        When the queue is empty, falls back to polling
        ``conversations.history`` on the bot's DM channel with the
        configured user, then for every top-level bot message also polls
        ``conversations.replies`` so thread replies are picked up (#1044).

        Slack's ``conversations.history`` and ``conversations.replies``
        do not stamp the ``channel`` field on each message — it is the
        request parameter, not part of the response. We stamp it here so
        downstream consumers (``SlackDmInboundScanner`` →
        ``PendingChatInjection.record``) have it (#1043).

        Only messages FROM the user are returned (bot's own messages are
        filtered out).
        """
        if self._dms:
            events, self._dms = self._dms, []
            return events
        if not self._user_id or not self._bot_token:
            return []
        channel = self.open_dm(self._user_id)
        if not channel:
            return []
        messages = self._poll_dm_history(channel=channel, since=since)
        bot_id = self._resolve_bot_id()
        return self._collect_user_dms(channel=channel, messages=messages, bot_id=bot_id)

    def _poll_dm_history(self, *, channel: str, since: str) -> list[RawAPIDict]:
        """Return the ``conversations.history`` messages list (or empty)."""
        params: dict[str, str | int] = {"channel": channel, "limit": 20}
        if since:
            params["oldest"] = since
        data = self._get("conversations.history", params)
        if not data.get("ok"):
            return []
        messages = data.get("messages")
        return [cast("RawAPIDict", m) for m in messages if isinstance(m, dict)] if isinstance(messages, list) else []

    def _collect_user_dms(
        self,
        *,
        channel: str,
        messages: list[RawAPIDict],
        bot_id: str,
    ) -> list[RawAPIDict]:
        """Filter bot-authored top-level posts; fan out to thread replies."""
        result: list[RawAPIDict] = []
        for msg in messages:
            msg.setdefault("channel", channel)
            if not _is_bot_authored(msg, bot_id):
                result.append(msg)
            if _is_thread_root(msg):
                result.extend(
                    self._fetch_thread_replies(channel=channel, thread_ts=str(msg["ts"]), bot_id=bot_id),
                )
        return result

    def _fetch_thread_replies(self, *, channel: str, thread_ts: str, bot_id: str) -> list[RawAPIDict]:
        """Return non-bot replies on a thread, with ``channel`` stamped."""
        data = self._get("conversations.replies", {"channel": channel, "ts": thread_ts, "limit": 50})
        if not data.get("ok"):
            return []
        messages = data.get("messages")
        if not isinstance(messages, list):
            return []
        replies: list[RawAPIDict] = []
        for m in messages:
            if not isinstance(m, dict):
                continue
            reply = cast("RawAPIDict", m)
            if reply.get("ts") == thread_ts or _is_bot_authored(reply, bot_id):
                continue
            reply.setdefault("channel", channel)
            replies.append(reply)
        return replies

    def _resolve_bot_id(self) -> str:
        if self._cached_bot_id is None:
            data = self._post("auth.test", {})
            self._cached_bot_id = str(data.get("user_id", "")) if data.get("ok") else ""
        return self._cached_bot_id

    def auth_test(self) -> RawAPIDict:
        """Return the raw ``auth.test`` response (``{}`` when no bot token).

        A connector preflight calls this to assert the bot token is live
        and correctly scoped before the loop proceeds — a hard-fail gate
        rather than discovering ``missing_scope`` mid-tick via a phantom
        ``post_message`` success.
        """
        return self._post("auth.test", {})

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        payload: SlackPayload = {"channel": channel, "text": text}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        return self._post("chat.postMessage", payload)

    def post_reply(self, *, channel: str, ts: str, text: str) -> RawAPIDict:
        return self._post("chat.postMessage", {"channel": channel, "thread_ts": ts, "text": text})

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        return self._post(
            "reactions.add",
            {"channel": channel, "timestamp": ts, "name": emoji},
            token=self._reaction_token(),
        )

    def open_dm(self, user_id: str) -> str:
        """Open a direct-message channel with *user_id* and return its channel id."""
        data = self._post("conversations.open", {"users": user_id})
        if not data.get("ok"):
            return ""
        channel = cast("RawAPIDict", data.get("channel") or {})
        channel_id = channel.get("id")
        return channel_id if isinstance(channel_id, str) else ""

    def get_permalink(self, *, channel: str, ts: str) -> str:
        """Return the archive permalink for ``(channel, ts)`` or ``""``."""
        if not channel or not ts:
            return ""
        data = self._get("chat.getPermalink", {"channel": channel, "message_ts": ts})
        if not data.get("ok"):
            return ""
        permalink = data.get("permalink", "")
        return permalink if isinstance(permalink, str) else ""

    def get_reactions(self, *, channel: str, ts: str) -> list[str]:
        """Return the emoji names currently set on a message."""
        data = self._get(
            "reactions.get",
            {"channel": channel, "timestamp": ts},
            token=self._reaction_token(),
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

    def resolve_user_id(self, handle: str) -> str:
        """Look up a Slack user id from a handle (``@alice`` or ``alice``)."""
        clean = handle.lstrip("@")
        if not clean:
            return ""
        data = self._get("users.lookupByEmail", {"email": clean}) if "@" in clean else {}
        if data.get("ok"):
            user = cast("RawAPIDict", data.get("user") or {})
            user_id = user.get("id")
            if isinstance(user_id, str):
                return user_id
        # Fallback: list users and match by name. Cheap for personal workspaces;
        # the loop scanners cache the result via ``functools.lru_cache`` upstream.
        listing = self._get("users.list", {"limit": 200})
        members = listing.get("members")
        if not isinstance(members, list):
            return ""
        for raw_member in members:
            if not isinstance(raw_member, dict):
                continue
            member = cast("RawAPIDict", raw_member)
            if member.get("name") == clean or member.get("real_name") == clean:
                user_id = member.get("id")
                if isinstance(user_id, str):
                    return user_id
        return ""
