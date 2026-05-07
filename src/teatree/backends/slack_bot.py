"""``SlackBotBackend`` — Slack messaging via the Web API.

Implements :class:`teatree.backends.protocols.MessagingBackend`. Outbound posts,
reactions, and user-id resolution use the Web API directly via httpx; inbound
``fetch_mentions`` / ``fetch_dms`` stream live events through Socket Mode and
are wired up by the ``t3 setup slack-bot`` walkthrough (see BLUEPRINT § 3.6).

The Phase 3 surface is intentionally minimal: enough for outbound routing
from the loop's scanners, with the Socket Mode receiver delivering inbound
events into the same backend through a queue managed by Phase 3.6.
"""

from typing import cast

import httpx

from teatree.core.sync import RawAPIDict

type SlackPayload = dict[str, object]


class SlackBotBackend:
    """MessagingBackend backed by a Slack bot token.

    ``bot_token`` (``xoxb-…``) authorises Web API calls.
    ``app_token`` (``xapp-…``) authorises Socket Mode and is consumed by the
    Phase 3.6 walkthrough — kept on the instance so the bot starter can pick
    it up without a second config read.
    ``user_id`` is the Slack user id of the human the bot speaks for; scanners
    use it to filter @mentions targeted at that user.
    """

    def __init__(self, *, bot_token: str = "", app_token: str = "", user_id: str = "") -> None:
        self._bot_token = bot_token
        self._app_token = app_token
        self._user_id = user_id
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

    def enqueue_mention(self, event: RawAPIDict) -> None:
        """Push a Socket Mode ``app_mention`` event into the inbound queue."""
        self._mentions.append(event)

    def enqueue_dm(self, event: RawAPIDict) -> None:
        """Push a Socket Mode ``message.im`` event into the inbound queue."""
        self._dms.append(event)

    def _post(self, method: str, payload: SlackPayload) -> RawAPIDict:
        if not self._bot_token:
            return {}
        response = httpx.post(
            f"https://slack.com/api/{method}",
            headers={
                "Authorization": f"Bearer {self._bot_token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json=payload,
            timeout=10.0,
        )
        response.raise_for_status()
        return cast("RawAPIDict", response.json())

    def _get(self, method: str, params: dict[str, str | int]) -> RawAPIDict:
        if not self._bot_token:
            return {}
        response = httpx.get(
            f"https://slack.com/api/{method}",
            headers={"Authorization": f"Bearer {self._bot_token}"},
            params=params,
            timeout=10.0,
        )
        response.raise_for_status()
        return cast("RawAPIDict", response.json())

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
        """Drain queued Socket Mode DMs. See :meth:`fetch_mentions`."""
        _ = since
        events, self._dms = self._dms, []
        return events

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        payload: SlackPayload = {"channel": channel, "text": text}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        return self._post("chat.postMessage", payload)

    def post_reply(self, *, channel: str, ts: str, text: str) -> RawAPIDict:
        return self._post("chat.postMessage", {"channel": channel, "thread_ts": ts, "text": text})

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        return self._post("reactions.add", {"channel": channel, "timestamp": ts, "name": emoji})

    def open_dm(self, user_id: str) -> str:
        """Open a direct-message channel with *user_id* and return its channel id."""
        data = self._post("conversations.open", {"users": user_id})
        if not data.get("ok"):
            return ""
        channel = cast("RawAPIDict", data.get("channel") or {})
        channel_id = channel.get("id")
        return channel_id if isinstance(channel_id, str) else ""

    def get_reactions(self, *, channel: str, ts: str) -> list[str]:
        """Return the emoji names currently set on a message."""
        data = self._get("reactions.get", {"channel": channel, "timestamp": ts})
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
