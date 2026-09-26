"""#1750 destination token routing for ``SlackBotBackend``, split out of ``bot.py``.

The self-DM-vs-colleague classifier and the token it selects — a private message
to the user (the user's own DM) goes through the per-overlay **bot** (``xoxb``);
a message or reaction to a *colleague* or a *channel* goes out under the user's
personal **OAuth** (``xoxp``) token — factored into free functions so ``bot.py``
stays under the module-health LOC cap. Distinct from the Connect-membership
policy (:func:`teatree.backends.slack.token_policy.channel_token`), which keeps
confirmed-internal channels (and *all* ``D…`` DMs) on the bot and cannot tell a
colleague DM from the self DM — exactly the distinction #1750 turns on.
"""

from collections.abc import Mapping

# ``channels:manage`` (granted so a dm_only bot can leave public channels) also permits these.
_CHANNEL_REACH_METHODS = frozenset({"conversations.join", "conversations.invite", "conversations.create"})


class OwnerDmOnlyError(RuntimeError):
    """A ``dm_only`` (owner-restricted) bot attempted an outbound to a non-owner surface.

    Raised by :func:`assert_owner_dm` before the send so a bot deliberately scoped
    to its one owner's DM can never leak a message or reaction into a colleague DM
    or a channel, even when a caller passes the wrong destination.
    """

    def __init__(self, channel: str, *, method: str = "") -> None:
        action = f"{method} to {channel!r}" if method else f"an outbound to {channel!r}"
        super().__init__(f"owner-restricted bot refused {action}: this bot may only reach its owner's own DM.")


def assert_owner_dm(channel: str, *, owner_dm_only: bool, dm_channel_id: str, user_id: str) -> None:
    """Raise :class:`OwnerDmOnlyError` when an owner-restricted bot targets a non-owner surface.

    A no-op unless *owner_dm_only* is set. Fail-closed: with no owner identity
    (*dm_channel_id* and *user_id* both empty) :func:`is_self_dm` is never true, so
    every destination is refused rather than falling open.
    """
    if owner_dm_only and not is_self_dm(channel, dm_channel_id=dm_channel_id, user_id=user_id):
        raise OwnerDmOnlyError(channel)


def assert_owner_call(
    method: str, payload: Mapping[str, object], *, owner_dm_only: bool, dm_channel_id: str, user_id: str
) -> None:
    """Refuse channel reach outright, and any post or DM open not addressed to the owner."""
    if not owner_dm_only:
        return
    if method in _CHANNEL_REACH_METHODS:
        raise OwnerDmOnlyError(str(payload.get("channel") or payload.get("name") or ""), method=method)
    if method == "chat.postMessage":
        channel = str(payload.get("channel") or "")
        if not is_self_dm(channel, dm_channel_id=dm_channel_id, user_id=user_id):
            raise OwnerDmOnlyError(channel, method=method)
    elif method == "conversations.open":
        users = str(payload.get("users") or "")
        if not user_id or users != user_id:
            raise OwnerDmOnlyError(users, method=method)


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


def select_routed_token(channel: str, *, dm_channel_id: str, user_id: str, bot_token: str, user_token: str) -> str:
    """The token a #1750-routed post/react to *channel* goes out under.

    A private message *to the user* — the user's own DM — goes through the bot
    (``xoxb``); a message or reaction to a *colleague* or a *channel* goes out
    under the user's personal OAuth (``xoxp``) token. Falls back to whichever
    single credential is configured when the other is absent, so a bot-only or
    user-only deployment still has a usable token.
    """
    if is_self_dm(channel, dm_channel_id=dm_channel_id, user_id=user_id):
        return bot_token or user_token
    return user_token or bot_token
