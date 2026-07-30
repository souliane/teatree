"""The single deterministic Slack token-selection policy (#1084, #1110).

Extracted verbatim from ``SlackBotBackend._channel_token``. The policy
is consulted for two operation classes, distinguished by the required
keyword-only ``op``, and it routes reads and writes to *different*
tokens by design.

Reads go out under the USER token. A ``SlackOp.READ`` is a genuine
history/metadata read taken to *view* a channel — the MCP
``slack_channel_history`` tool, an interactive "what's in this channel"
read. The bot token (``xoxb-…``) reads only channels the bot was
invited to, so a bot-token read reports ``[]`` for every channel the
user is in but the bot is not — the agent silently misses the user's
own channels. Routing a general read through the user OAuth token
(``xoxp-…``) makes the agent see exactly the channels and history the
user sees. The only read kept on the bot is a DM read (see below).

Writes go out under the interaction token. A ``SlackOp.WRITE`` is a
``chat.postMessage`` / reaction, OR a read taken *as the post* — the
#1084 review-request dedup guard reads channel history and must read
with the SAME token its subsequent post/reaction will go out under
(read-token == post-token). That guard passes ``SlackOp.WRITE``, so it
flows the WRITE branch and is unchanged by the read-as-user rule above:
a Connect channel rejects the bot token (``xoxb-…``) with
``mcp_externally_shared_channel_restricted`` for *both* posting and
reading, so a dedup that read under the bot would see an empty history
and never suppress a duplicate. Read-token == post-token stays a
load-bearing correctness invariant on the WRITE path.

The write-side fail-toward-user invariant (#1110). On the WRITE path a
confirmed Connect channel routes to the user ``xoxp`` (the bot is
rejected outright), a confirmed internal channel interacts as the bot
``xoxb``, and an *ambiguous* channel (Connect membership could not be
confirmed) fails *toward* the user ``xoxp`` — the only token that can
reach a Connect channel — never silently toward a bot the channel may
reject.

``is_ext_shared`` is tri-state: ``True`` (confirmed externally-shared),
``False`` (confirmed internal), or ``None`` (membership could not be
confirmed — ``conversations.info`` failed). Only the WRITE path
consults ``is_ext_shared``; a READ routes to the user token on any
non-DM channel without probing membership. The single-credential and
DM short-circuits are bit-for-bit unchanged from #1084, and the whole
policy activates only when a user token is configured (the
``if not user_token`` short-circuit) — a bot-token-only deployment is
unchanged.
"""

from enum import Enum


class SlackOp(Enum):
    """The operation class an outbound token request is for (#1110).

    ``READ`` is a genuine history / metadata read taken to *view* a
    channel (routed to the user token so the agent sees what the user
    sees); ``WRITE`` is a post, reaction, or a read taken *as the post*
    (the #1084 dedup guard — routed to the interaction token).
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
    single-credential case; nothing to route to — the whole read-as-user
    policy activates only once a user token exists).

    No bot token configured -> user token. There is no second credential
    to probe Connect membership with, and the user-token-only deployment
    intends every call to go out under the user's identity anyway.

    A DM channel (id starts with ``D``) -> bot token. DMs are scoped to
    the bot's own IM channels; a user-token read of the bot's DM history
    would be wrong, and routing a DM post through the user token would
    impersonate the user against their own DM history.

    A ``SlackOp.READ`` on any non-DM channel -> user token. A general
    history/metadata read routes through the user's identity so the
    agent sees exactly the channels and history the user sees — the bot
    token reads only the channels the bot was invited to.

    The remaining cases are all WRITE (a post, a reaction, or a read
    taken *as the post* — the #1084 dedup guard):

    A *confirmed* Slack-Connect externally-shared channel
    (``is_ext_shared is True``) -> user token. The bot token is rejected
    there with ``mcp_externally_shared_channel_restricted``; the user's
    ``xoxp`` is a partner-channel member and can post and read.

    A *confirmed* ordinary internal channel (``is_ext_shared is False``)
    -> bot token — interact as the bot.

    An *ambiguous* channel (``is_ext_shared is None`` — Connect
    membership could not be confirmed) -> user token. A silent bot-token
    write to a Connect channel is rejected and the partner write is
    dropped, so an ambiguous write fails *toward* the user ``xoxp`` — the
    only token that can reach a Connect channel.
    """
    if not user_token:
        return bot_token
    if not bot_token:
        return user_token
    if channel.startswith("D"):
        return bot_token
    if op is SlackOp.READ:
        return user_token
    # WRITE: only a confirmed-internal channel interacts as the bot; a confirmed
    # Connect (True) or an unconfirmed (None) channel fails toward the user xoxp —
    # the only token that can reach a Connect channel the bot would be rejected on.
    return bot_token if is_ext_shared is False else user_token
