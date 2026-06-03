"""``NoopMessagingBackend`` — default for overlays that don't declare a chat backend."""

from teatree.types import RawAPIDict


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
    def fetch_reactions(*, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    @staticmethod
    def fetch_message(*, channel: str, ts: str) -> RawAPIDict:
        _ = channel, ts
        return {}

    @staticmethod
    def fetch_channel_history(*, channel: str, limit: int = 50) -> list[RawAPIDict]:
        _ = channel, limit
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
    def upload_audio_to_dm(*, channel: str, filepath: str, title: str = "") -> RawAPIDict:
        _ = channel, filepath, title
        return {}
