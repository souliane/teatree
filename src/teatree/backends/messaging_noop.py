"""``NoopMessagingBackend`` — default for overlays that don't declare a chat backend."""

from teatree.core.sync import RawAPIDict


class NoopMessagingBackend:
    """Inert MessagingBackend — silently drops outbound, returns nothing inbound.

    Selected when an overlay sets ``messaging_backend = "noop"`` (the default)
    or omits the field entirely. Allows agents and scanners to call the
    Protocol uniformly without per-call ``if backend is not None`` guards.
    """

    @staticmethod
    def fetch_mentions(*, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    @staticmethod
    def fetch_dms(*, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    @staticmethod
    def post_message(*, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        _ = channel, text, thread_ts
        return {}

    @staticmethod
    def post_reply(*, channel: str, ts: str, text: str) -> RawAPIDict:
        _ = channel, ts, text
        return {}

    @staticmethod
    def react(*, channel: str, ts: str, emoji: str) -> RawAPIDict:
        _ = channel, ts, emoji
        return {}

    @staticmethod
    def resolve_user_id(handle: str) -> str:
        _ = handle
        return ""
