"""``NoopMessagingBackend`` — default for overlays that don't declare a chat backend."""

from typing import ClassVar

from teatree.types import ChannelReadRefusedError, RawAPIDict


class NoopMessagingBackend:
    """Inert MessagingBackend — silently drops outbound, returns nothing inbound.

    Selected when an overlay sets ``messaging_backend = "noop"`` (the default)
    or omits the field entirely. Allows agents and scanners to call the
    Protocol uniformly without per-call ``if backend is not None`` guards.

    ``is_noop`` is the capability marker ``core`` consults where a REAL
    transport is required (owner DMs, ``resolve_owner_dm_backend``): ``core``
    cannot import this class (the core → backends DAG cut, #1922), so the
    marker travels on the instance instead of an ``isinstance`` check.
    """

    is_noop: ClassVar[bool] = True

    @staticmethod
    def fetch_mentions(*, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    @staticmethod
    def fetch_dms(*, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    @staticmethod
    def fetch_reactions(*, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    @staticmethod
    def fetch_message(*, channel: str, ts: str) -> RawAPIDict:
        _ = channel, ts
        return {}

    @staticmethod
    def fetch_thread_replies(*, channel: str, thread_ts: str) -> list[RawAPIDict]:
        _ = channel, thread_ts
        return []

    @staticmethod
    def fetch_channel_history(*, channel: str, limit: int = 50) -> list[RawAPIDict]:
        _ = channel, limit
        return []

    @staticmethod
    def fetch_channel_history_or_refuse(*, channel: str, limit: int = 50) -> list[RawAPIDict]:
        _ = limit
        raise ChannelReadRefusedError(channel, "noop_messaging_backend")

    @staticmethod
    def post_message(
        *,
        channel: str,
        text: str,
        thread_ts: str = "",
        blocks: list[RawAPIDict] | None = None,
    ) -> RawAPIDict:
        _ = channel, text, thread_ts, blocks
        return {}

    @staticmethod
    def post_reply(*, channel: str, ts: str, text: str) -> RawAPIDict:
        _ = channel, ts, text
        return {}

    @staticmethod
    def open_dm(user_id: str) -> str:
        _ = user_id
        return ""

    @staticmethod
    def get_permalink(*, channel: str, ts: str) -> str:
        _ = channel, ts
        return ""

    @staticmethod
    def react(*, channel: str, ts: str, emoji: str) -> RawAPIDict:
        _ = channel, ts, emoji
        return {}

    @staticmethod
    def post_routed(*, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        _ = channel, text, thread_ts
        return {}

    @staticmethod
    def react_routed(*, channel: str, ts: str, emoji: str) -> RawAPIDict:
        _ = channel, ts, emoji
        return {}

    @staticmethod
    def resolve_user_id(handle: str) -> str:
        _ = handle
        return ""

    @staticmethod
    def auth_test() -> RawAPIDict:
        return {}

    @staticmethod
    def post_audio_dm(*, channel: str, filepath: str, text: str, thread_ts: str = "", title: str = "") -> RawAPIDict:
        _ = channel, filepath, text, thread_ts, title
        return {}
