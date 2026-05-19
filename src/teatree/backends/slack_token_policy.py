"""The single deterministic Slack token-selection policy (#1084, #1110).

Extracted verbatim from ``SlackBotBackend._channel_token`` so the
review-request dedup guard reads channel history with the **same** token
the post will go out under. A Slack-Connect externally-shared channel
rejects the bot token (``xoxb-…``) with
``mcp_externally_shared_channel_restricted`` for *both* posting and
reading history — so the guard's live read MUST use the user OAuth token
(``xoxp-…``) exactly when the post would. Read-token == post-token is a
load-bearing correctness invariant: a dedup that reads with a token the
channel rejects would always see an empty history and never suppress a
duplicate.

The reads-fail-safe-to-bot invariant (#1110). The policy is consulted
for two operation classes, distinguished by the required keyword-only
``op``. A ``SlackOp.READ`` is a history/metadata read: the bot token
can always read its own metadata, and on an *unreachable* Connect
channel a bot-token history read returns an empty list at worst (the
#1084 dedup tolerates that), so a READ on an *ambiguous* channel
(Connect membership could not be confirmed) stays conservative on the
bot token — reads fail safe to the bot. A ``SlackOp.WRITE`` is a
``chat.postMessage`` / reaction / a read taken *as the post* (the
#1084 guard): a Connect channel rejects the bot token outright, so a
silent bot-token write would post under the wrong identity or drop the
partner write entirely, hence a WRITE on an *ambiguous* channel fails
*toward* the user ``xoxp`` token (the only token that can reach a
Connect channel), never silently toward the bot. Writes / reactions in
a shared or ambiguous context fail toward the user xoxp.

``is_ext_shared`` is tri-state: ``True`` (confirmed externally-shared),
``False`` (confirmed internal), or ``None`` (membership could not be
confirmed — ``conversations.info`` failed). Only the ``None`` case
consults ``op``; confirmed ``True`` / ``False`` and the
single-credential / DM short-circuits are bit-for-bit unchanged from
#1084.
"""

from enum import Enum


class SlackOp(Enum):
    """The operation class an outbound token request is for (#1110).

    ``READ`` is a history / metadata read; ``WRITE`` is a post,
    reaction, or a read taken *as the post* (the #1084 dedup guard).
    Only the ambiguous (``is_ext_shared is None``) branch consults it.
    """

    READ = "read"
    WRITE = "write"


def channel_token(
    channel: str,
    *,
    bot_token: str,
    user_token: str,
    is_ext_shared: bool | None,
    op: SlackOp,
) -> str:
    """The token authorising an outbound call to (or a history read of) *channel*.

    No user (``xoxp``) token configured -> bot token (the legacy
    single-credential case; nothing to route to).

    No bot token configured -> user token. There is no second credential
    to probe Connect membership with, and the user-token-only deployment
    intends every call to go out under the user's identity anyway.

    A DM channel (id starts with ``D``) -> bot token. DMs are scoped to
    the bot's own IM channels; routing them through the user token would
    impersonate the user against their own DM history.

    A *confirmed* Slack-Connect externally-shared channel
    (``is_ext_shared is True``) -> user token. The bot token is rejected
    there with ``mcp_externally_shared_channel_restricted``; the user's
    ``xoxp`` is a partner-channel member and can post and read.

    A *confirmed* ordinary internal channel (``is_ext_shared is False``)
    -> bot token.

    An *ambiguous* channel (``is_ext_shared is None`` — Connect
    membership could not be confirmed): ``op`` decides. A
    ``SlackOp.READ`` falls safe to the bot token (reads fail safe to the
    bot — a bot-token read of an unreachable Connect channel is empty at
    worst, which the #1084 dedup tolerates). A ``SlackOp.WRITE`` fails
    *toward* the user ``xoxp`` token (writes / reactions in a shared or
    ambiguous context fail toward the user xoxp — a silent bot-token
    write to a Connect channel is rejected and the partner write is
    dropped).
    """
    if not user_token:
        return bot_token
    if not bot_token:
        return user_token
    if channel.startswith("D"):
        return bot_token
    if is_ext_shared is True:
        return user_token
    if is_ext_shared is False:
        return bot_token
    return bot_token if op is SlackOp.READ else user_token
