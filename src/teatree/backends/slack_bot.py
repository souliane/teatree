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

    @property
    def app_token(self) -> str:
        return self._app_token

    @property
    def user_id(self) -> str:
        return self._user_id

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

    @staticmethod
    def fetch_mentions(*, since: str = "") -> list[RawAPIDict]:
        """Inbound mentions stream through Socket Mode (Phase 3.6).

        Returns the empty list until the Phase 3.6 receiver is active —
        scanners call this every tick and tolerate an empty result.
        """
        _ = since
        return []

    @staticmethod
    def fetch_dms(*, since: str = "") -> list[RawAPIDict]:
        """Inbound DMs stream through Socket Mode (Phase 3.6).

        See :meth:`fetch_mentions` — same contract.
        """
        _ = since
        return []

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        payload: SlackPayload = {"channel": channel, "text": text}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        return self._post("chat.postMessage", payload)

    def post_reply(self, *, channel: str, ts: str, text: str) -> RawAPIDict:
        return self._post("chat.postMessage", {"channel": channel, "thread_ts": ts, "text": text})

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        return self._post("reactions.add", {"channel": channel, "timestamp": ts, "name": emoji})

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
