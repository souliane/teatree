"""#1750 destination token routing for ``SlackBotBackend``, split out of ``bot.py``.

The self-DM-vs-colleague classifier and the token it selects — a private message
to the user (the user's own DM) goes through the per-overlay **bot** (``xoxb``);
a message or reaction to a registered operator goes through the bot; other
colleague or channel messages use the user's **OAuth** (``xoxp``) token. This split keeps ``bot.py``
under the module-health LOC cap. Distinct from the Connect-membership
policy (:func:`teatree.backends.slack.token_policy.channel_token`), which keeps
confirmed-internal channels (and *all* ``D…`` DMs) on the bot and cannot tell a
colleague DM from the self DM — exactly the distinction #1750 turns on.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from teatree.types import RawAPIDict

# ``channels:manage`` (granted so a dm_only bot can leave public channels) also permits these.
_CHANNEL_REACH_METHODS = frozenset({"conversations.join", "conversations.invite", "conversations.create"})


@dataclass(frozen=True, slots=True)
class OperatorDms:
    user_ids: frozenset[str] = frozenset()
    channels: frozenset[str] = frozenset()

    def snapshot(self) -> "OperatorDms":
        return self


_NO_OPERATOR_DMS = OperatorDms()


class SlackInfoReader(Protocol):
    def __call__(self, method: str, params: dict[str, str | int], *, token: str = "") -> RawAPIDict: ...


@dataclass(slots=True)
class OperatorDmRegistry:
    resolver: Callable[[], frozenset[str]] = frozenset
    channels: dict[str, str] = field(default_factory=dict)

    def configure(self, resolver: Callable[[], frozenset[str]]) -> None:
        self.resolver = resolver

    def snapshot(self) -> OperatorDms:
        users = self.resolver()
        return OperatorDms(
            user_ids=users,
            channels=frozenset(channel for channel, user in self.channels.items() if user in users),
        )

    def remember(self, user_id: str, channel: str) -> None:
        if channel and user_id in self.resolver():
            self.channels[channel] = user_id

    def remember_verified_channel(
        self, channel: str, *, owner_channel: str, read_info: SlackInfoReader, token: str
    ) -> None:
        if not channel.startswith("D") or channel == owner_channel:
            return
        operators = self.snapshot()
        if not operators.user_ids or channel in operators.channels:
            return
        info = read_info("conversations.info", {"channel": channel}, token=token).get("channel")
        if isinstance(info, dict) and info.get("id") == channel and info.get("is_im") is True:
            self.remember(str(info.get("user") or ""), channel)


@dataclass(frozen=True, slots=True)
class DmGuard:
    restricted: bool
    dm_channel_id: str
    user_id: str
    operators: OperatorDms | OperatorDmRegistry = _NO_OPERATOR_DMS


class OwnerDmOnlyError(RuntimeError):
    """A ``dm_only`` (owner-restricted) bot attempted an outbound to a non-owner surface.

    Raised by :func:`assert_owner_dm` before the send so a bot deliberately scoped
    to its one owner's DM can never leak a message or reaction into a colleague DM
    or a channel, even when a caller passes the wrong destination.
    """

    def __init__(self, channel: str, *, method: str = "") -> None:
        action = f"{method} to {channel!r}" if method else f"an outbound to {channel!r}"
        super().__init__(f"owner-restricted bot refused {action}: this bot may only reach its owner's own DM.")


def assert_owner_dm(
    channel: str,
    *,
    owner_dm_only: bool,
    dm_channel_id: str,
    user_id: str,
    operator_dms: OperatorDms | OperatorDmRegistry = _NO_OPERATOR_DMS,
) -> None:
    """Raise :class:`OwnerDmOnlyError` when an owner-restricted bot targets a non-owner surface.

    A no-op unless *owner_dm_only* is set. Fail-closed: with no owner identity
    (*dm_channel_id* and *user_id* both empty) :func:`is_self_dm` is never true, so
    every destination is refused rather than falling open.
    """
    if owner_dm_only and not _allowed_dm(channel, guard=DmGuard(owner_dm_only, dm_channel_id, user_id, operator_dms)):
        raise OwnerDmOnlyError(channel)


def assert_owner_call(method: str, payload: Mapping[str, object], *, guard: DmGuard) -> None:
    """Refuse channel reach outright, and any post, edit or DM open not addressed to the owner."""
    if not guard.restricted:
        return
    if method in _CHANNEL_REACH_METHODS:
        raise OwnerDmOnlyError(str(payload.get("channel") or payload.get("name") or ""), method=method)
    if method in {"chat.postMessage", "chat.update"}:
        channel = str(payload.get("channel") or "")
        if not _allowed_dm(channel, guard=guard):
            raise OwnerDmOnlyError(channel, method=method)
    elif method == "conversations.open":
        users = str(payload.get("users") or "")
        if not users or (users != guard.user_id and users not in guard.operators.snapshot().user_ids):
            raise OwnerDmOnlyError(users, method=method)


def _allowed_dm(channel: str, *, guard: DmGuard) -> bool:
    if is_self_dm(channel, dm_channel_id=guard.dm_channel_id, user_id=guard.user_id):
        return True
    operators = guard.operators.snapshot()
    return bool(channel) and (channel in operators.user_ids or channel in operators.channels)


def is_self_dm(channel: str, *, dm_channel_id: str, user_id: str) -> bool:
    """True when *channel* is the configured user's own DM (#1750).

    The single deterministic destination test for the #1750 routing rule. The
    user's own IM is the channel id provisioned at ``t3 setup`` time
    (*dm_channel_id*), or — when an ``open_dm`` has not yet been resolved — the
    user's own ``U…`` id, which Slack accepts as a ``chat.postMessage`` target
    that opens/uses the self-IM. A *colleague's* DM is a different ``D…`` id and
    is therefore NOT a self-DM, so it routes to ``xoxp`` like any other non-self
    surface.
    """
    if not channel:
        return False
    if dm_channel_id and channel == dm_channel_id:
        return True
    return bool(user_id) and channel == user_id


def select_routed_token(channel: str, *, guard: DmGuard, bot_token: str, user_token: str) -> str:
    """The token a #1750-routed post/react to *channel* goes out under.

    A private message *to the user* — the user's own DM — goes through the bot
    (``xoxb``); a registered operator's DM also uses the bot. Other colleague
    or channel messages use the user's OAuth (``xoxp``) token. Falls back to whichever
    single credential is configured when the other is absent, so a bot-only or
    user-only deployment still has a usable token.
    """
    if is_self_dm(channel, dm_channel_id=guard.dm_channel_id, user_id=guard.user_id):
        return bot_token or user_token
    operators = guard.operators.snapshot()
    if channel in operators.user_ids or channel in operators.channels:
        return bot_token
    return user_token or bot_token
