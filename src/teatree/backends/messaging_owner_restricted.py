"""Owner-DM-restricted messaging wrapper for the ``dm_only`` scope profile.

A composition wrapper that hard-restricts a messaging backend to the overlay
owner's own DM. Any outbound write — or channel-scoped history read — aimed at a
channel that is not the owner's self-DM raises :class:`OwnerDmOnlyError`, so a
DM-only overlay (e.g. ``t3-teatree``, whose bot exists only to talk to its one
human) can never leak a message into a colleague DM or a channel even when a
caller passes the wrong destination. This is the runtime half of the ``dm_only``
profile — the manifest half (:mod:`teatree.cli.slack.manifest`) narrows the app's
Slack scopes so the same calls also fail at the API; the wrapper is the belt to
that suspenders, failing LOUD in-process before the call is ever made.

Non-destination calls (``auth_test``, ``resolve_user_id``, ``fetch_dms``,
``fetch_mentions``, ``get_permalink``, …) and any attribute the inner backend
exposes but this wrapper does not name (e.g. ``set_voice_classifier_mode``) are
delegated to the inner backend unchanged. ``auth_test`` in particular passes
through so the account-switch connector probe sees the real backend's live
reachability rather than a wrapper artefact.

Fail-closed identity: the self-DM test needs the owner's identity
(``dm_channel_id`` and/or ``user_id``). When neither is configured, *every*
destination is "not the owner" and all guarded calls raise — a restricted bot
with no known owner refuses to send anywhere rather than fall open.
"""

from teatree.backends.slack.routing import is_self_dm
from teatree.core.backend_protocols import MessagingBackend
from teatree.types import RawAPIDict


class OwnerDmOnlyError(RuntimeError):
    """A ``dm_only`` overlay attempted an outbound to a non-owner destination."""


class OwnerRestrictedMessaging:
    """Wrap *inner*, refusing any guarded call whose channel is not the owner's self-DM.

    Guarded (channel-scoped) methods raise :class:`OwnerDmOnlyError` unless
    the target channel is the owner's own DM (:func:`is_self_dm`); ``open_dm``
    raises unless the target user is the owner. Everything else — reads that are
    inherently owner-scoped, identity/permalink helpers, ``auth_test``, and any
    unnamed inner attribute — delegates to *inner*.
    """

    def __init__(self, inner: MessagingBackend, *, dm_channel_id: str = "", user_id: str = "") -> None:
        self._inner = inner
        self._dm_channel_id = dm_channel_id
        self._user_id = user_id

    def __getattr__(self, name: str) -> object:
        # Reached only for attributes this wrapper does not define — delegate to
        # the inner backend (e.g. ``set_voice_classifier_mode``, ``name``). Guard
        # ``_inner`` itself so a lookup during ``__init__`` cannot recurse.
        if name == "_inner":
            raise AttributeError(name)
        return getattr(self._inner, name)

    def _assert_owner_dm(self, channel: str) -> None:
        if not is_self_dm(channel, dm_channel_id=self._dm_channel_id, user_id=self._user_id):
            msg = (
                f"dm_only overlay refused an outbound to {channel!r}: this bot is restricted "
                "to the owner's own DM and may not post to or read any other channel."
            )
            raise OwnerDmOnlyError(msg)

    # ── Guarded channel-scoped reads ─────────────────────────────────────────
    def fetch_message(self, *, channel: str, ts: str) -> RawAPIDict:
        self._assert_owner_dm(channel)
        return self._inner.fetch_message(channel=channel, ts=ts)

    def fetch_thread_replies(self, *, channel: str, thread_ts: str) -> list[RawAPIDict]:
        self._assert_owner_dm(channel)
        return self._inner.fetch_thread_replies(channel=channel, thread_ts=thread_ts)

    def fetch_channel_history(self, *, channel: str, limit: int = 50) -> list[RawAPIDict]:
        self._assert_owner_dm(channel)
        return self._inner.fetch_channel_history(channel=channel, limit=limit)

    # ── Guarded writes ───────────────────────────────────────────────────────
    def post_message(
        self,
        *,
        channel: str,
        text: str,
        thread_ts: str = "",
        blocks: list[RawAPIDict] | None = None,
    ) -> RawAPIDict:
        self._assert_owner_dm(channel)
        return self._inner.post_message(channel=channel, text=text, thread_ts=thread_ts, blocks=blocks)

    def post_reply(self, *, channel: str, ts: str, text: str) -> RawAPIDict:
        self._assert_owner_dm(channel)
        return self._inner.post_reply(channel=channel, ts=ts, text=text)

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        self._assert_owner_dm(channel)
        return self._inner.react(channel=channel, ts=ts, emoji=emoji)

    def post_routed(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        self._assert_owner_dm(channel)
        return self._inner.post_routed(channel=channel, text=text, thread_ts=thread_ts)

    def react_routed(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        self._assert_owner_dm(channel)
        return self._inner.react_routed(channel=channel, ts=ts, emoji=emoji)

    def post_audio_dm(
        self,
        *,
        channel: str,
        filepath: str,
        text: str,
        thread_ts: str = "",
        title: str = "",
    ) -> RawAPIDict:
        self._assert_owner_dm(channel)
        return self._inner.post_audio_dm(
            channel=channel, filepath=filepath, text=text, thread_ts=thread_ts, title=title
        )

    def open_dm(self, user_id: str) -> str:
        # ``open_dm`` has no channel yet — the owner is named by user id. Only the
        # owner's own id may be opened; a colleague id is refused.
        if not (self._user_id and user_id == self._user_id):
            msg = f"dm_only overlay refused open_dm({user_id!r}): this bot may open only the owner's own DM."
            raise OwnerDmOnlyError(msg)
        return self._inner.open_dm(user_id)

    # ── Unguarded (owner-scoped or destination-free) pass-throughs ───────────
    def fetch_mentions(self, *, since: str = "") -> list[RawAPIDict]:
        return self._inner.fetch_mentions(since=since)

    def fetch_dms(self, *, since: str = "") -> list[RawAPIDict]:
        return self._inner.fetch_dms(since=since)

    def fetch_reactions(self, *, since: str = "") -> list[RawAPIDict]:
        return self._inner.fetch_reactions(since=since)

    def get_permalink(self, *, channel: str, ts: str) -> str:
        return self._inner.get_permalink(channel=channel, ts=ts)

    def resolve_user_id(self, handle: str) -> str:
        return self._inner.resolve_user_id(handle)

    def auth_test(self) -> RawAPIDict:
        return self._inner.auth_test()


__all__ = ["OwnerDmOnlyError", "OwnerRestrictedMessaging"]
