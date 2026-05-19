"""The single deterministic Slack token-selection policy (#1084).

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
"""


def channel_token(channel: str, *, bot_token: str, user_token: str, is_ext_shared: bool) -> str:
    """The token authorising an outbound call to (or a history read of) *channel*.

    No user (``xoxp``) token configured -> bot token (the legacy
    single-credential case; nothing to route to).

    No bot token configured -> user token. There is no second credential
    to probe Connect membership with, and the user-token-only deployment
    intends every call to go out under the user's identity anyway.

    A DM channel (id starts with ``D``) -> bot token. DMs are scoped to
    the bot's own IM channels; routing them through the user token would
    impersonate the user against their own DM history.

    A Slack-Connect externally-shared channel -> user token. The bot
    token is rejected there with
    ``mcp_externally_shared_channel_restricted``; the user's ``xoxp`` is
    a partner-channel member and can post and read.

    Any ordinary internal channel -> bot token.
    """
    if not user_token:
        return bot_token
    if not bot_token:
        return user_token
    if channel.startswith("D"):
        return bot_token
    return user_token if is_ext_shared else bot_token
