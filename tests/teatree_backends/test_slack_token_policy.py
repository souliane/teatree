"""The single Slack token-selection policy (#1084, #1110).

Reads and writes route to *different* tokens by design. A genuine
``SlackOp.READ`` (a history/metadata read taken to VIEW a channel — the
MCP ``slack_channel_history`` tool) goes out under the USER ``xoxp``
token on any non-DM channel, so the agent sees the same channels and
history the user sees rather than the bot's view of only the channels
it was invited to. A ``SlackOp.WRITE`` (a post, a reaction, or the
#1084 dedup guard's read-taken-as-the-post) keeps the interaction
doctrine: confirmed Connect -> user, confirmed internal -> bot,
ambiguous -> user (a bot-token write to a Connect channel is rejected
and the partner write is dropped). DMs and the single-credential
short-circuits are bit-for-bit unchanged from #1084.
"""

from teatree.backends.slack.token_policy import SlackOp, channel_token


def test_no_user_token_returns_bot_token() -> None:
    assert channel_token("C1", bot_token="xoxb", user_token="", is_ext_shared=True, op=SlackOp.WRITE) == "xoxb"


def test_no_bot_token_returns_user_token() -> None:
    assert channel_token("C1", bot_token="", user_token="xoxp", is_ext_shared=False, op=SlackOp.READ) == "xoxp"


def test_dm_read_still_bot() -> None:
    """A DM read stays on the bot — a user-token read of the bot's own DM history would be wrong."""
    assert channel_token("D123", bot_token="xoxb", user_token="xoxp", is_ext_shared=None, op=SlackOp.READ) == "xoxb"


def test_dm_write_still_bot() -> None:
    assert channel_token("D123", bot_token="xoxb", user_token="xoxp", is_ext_shared=True, op=SlackOp.WRITE) == "xoxb"


def test_read_on_confirmed_internal_returns_user_token() -> None:
    """A general READ sees what the USER sees — the intent flip.

    The pre-flip policy routed a confirmed-internal READ to the bot
    token (the bot's view of only channels it was invited to). The new
    intent: a genuine viewing read routes through the user ``xoxp`` so
    the agent sees the same channels the user does.
    """
    assert channel_token("C9", bot_token="xoxb", user_token="xoxp", is_ext_shared=False, op=SlackOp.READ) == "xoxp"


def test_read_on_ambiguous_returns_user_token() -> None:
    """Unknown Connect membership + READ -> user token (was bot pre-flip).

    A READ never consults ``is_ext_shared`` under the new policy — it
    routes to the user token on any non-DM channel. This replaces the
    old #1110 "ambiguous READ falls safe to the bot" behaviour, which
    was deliberately narrowed to the WRITE path.
    """
    assert channel_token("C9", bot_token="xoxb", user_token="xoxp", is_ext_shared=None, op=SlackOp.READ) == "xoxp"


def test_read_on_confirmed_ext_shared_returns_user_token() -> None:
    assert channel_token("C9", bot_token="xoxb", user_token="xoxp", is_ext_shared=True, op=SlackOp.READ) == "xoxp"


def test_confirmed_ext_shared_write_still_user() -> None:
    assert channel_token("C9", bot_token="xoxb", user_token="xoxp", is_ext_shared=True, op=SlackOp.WRITE) == "xoxp"


def test_confirmed_internal_write_still_bot() -> None:
    """A WRITE to a confirmed-internal channel interacts as the bot — unchanged by the read flip."""
    assert channel_token("C9", bot_token="xoxb", user_token="xoxp", is_ext_shared=False, op=SlackOp.WRITE) == "xoxb"


def test_ambiguous_channel_write_returns_user_token() -> None:
    """Unknown Connect membership + WRITE -> user xoxp (the #1110 fix, preserved).

    An unconfirmed channel could be a Connect channel that rejects the
    bot token with ``mcp_externally_shared_channel_restricted`` and
    silently drops the post — so a write must fail toward the user token.
    """
    assert channel_token("C9", bot_token="xoxb", user_token="xoxp", is_ext_shared=None, op=SlackOp.WRITE) == "xoxp"


def test_dedup_read_as_post_uses_the_posts_token() -> None:
    """The #1084 dedup guard reads history AS the post, so it passes WRITE.

    Read-token == post-token stays load-bearing: the dedup read of a
    confirmed-internal channel must use the bot token the reaction will
    go out under (not the user token a genuine viewing READ would use),
    so the read sees the same history the post's identity does.
    """
    assert channel_token("C9", bot_token="xoxb", user_token="xoxp", is_ext_shared=False, op=SlackOp.WRITE) == "xoxb"
    assert channel_token("C9", bot_token="xoxb", user_token="xoxp", is_ext_shared=True, op=SlackOp.WRITE) == "xoxp"


def test_single_credential_unchanged() -> None:
    """The single-credential short-circuits are bit-for-bit unchanged.

    No user token -> bot regardless of op / membership; no bot token ->
    user regardless of op / membership.
    """
    assert channel_token("C9", bot_token="xoxb", user_token="", is_ext_shared=None, op=SlackOp.WRITE) == "xoxb"
    assert channel_token("C9", bot_token="xoxb", user_token="", is_ext_shared=None, op=SlackOp.READ) == "xoxb"
    assert channel_token("C9", bot_token="", user_token="xoxp", is_ext_shared=None, op=SlackOp.READ) == "xoxp"
    assert channel_token("C9", bot_token="", user_token="xoxp", is_ext_shared=None, op=SlackOp.WRITE) == "xoxp"
