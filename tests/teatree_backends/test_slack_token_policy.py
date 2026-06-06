"""The single Slack token-selection policy (#1084, #1110).

Extracted verbatim from ``SlackBotBackend._channel_token`` so the
review-request dedup guard reads channel history with the same token the
post will use (read-token == post-token). These assert each branch of
the policy directly; ``test_slack_connect_channel_routing.py`` proves the
``SlackBotBackend`` refactor stays behaviour-preserving.

#1110 adds a required keyword-only ``op`` (``SlackOp.READ`` /
``SlackOp.WRITE``) and an ``is_ext_shared`` tri-state. When Connect
membership is *unknown* (``conversations.info`` failed → ``None``) the
operation decides: a READ falls safe to the bot token (the bot can
always read its own metadata; the worst case is an empty history a
dedup tolerates), but a WRITE / reaction falls toward the user ``xoxp``
token, because a Connect channel rejects the bot token outright and a
silent bot-token write would post under the wrong identity or drop the
partner write entirely.
"""

from teatree.backends.slack.token_policy import SlackOp, channel_token


def test_no_user_token_returns_bot_token() -> None:
    assert channel_token("C1", bot_token="xoxb", user_token="", is_ext_shared=True, op=SlackOp.WRITE) == "xoxb"


def test_no_bot_token_returns_user_token() -> None:
    assert channel_token("C1", bot_token="", user_token="xoxp", is_ext_shared=False, op=SlackOp.READ) == "xoxp"


def test_dm_still_bot() -> None:
    assert channel_token("D123", bot_token="xoxb", user_token="xoxp", is_ext_shared=True, op=SlackOp.WRITE) == "xoxb"


def test_confirmed_ext_shared_write_still_user() -> None:
    assert channel_token("C9", bot_token="xoxb", user_token="xoxp", is_ext_shared=True, op=SlackOp.WRITE) == "xoxp"


def test_confirmed_internal_write_still_bot() -> None:
    assert channel_token("C9", bot_token="xoxb", user_token="xoxp", is_ext_shared=False, op=SlackOp.WRITE) == "xoxb"


def test_ambiguous_channel_write_returns_user_token() -> None:
    """Unknown Connect membership + WRITE -> user xoxp (the #1110 fix).

    The pre-#1110 policy had no ``op`` and routed an unknown channel
    (``is_ext_shared`` was hard ``False``) to the bot — Slack then
    rejects a Connect write with
    ``mcp_externally_shared_channel_restricted`` and the post is
    silently dropped. A write must fail toward the user token.
    """
    assert channel_token("C9", bot_token="xoxb", user_token="xoxp", is_ext_shared=None, op=SlackOp.WRITE) == "xoxp"


def test_ambiguous_channel_read_returns_bot_token() -> None:
    """Unknown Connect membership + READ -> bot token.

    A read with the bot token on an unreachable Connect channel returns
    an empty history at worst — which the #1084 dedup tolerates. The bot
    can always read its own metadata, so READ stays conservative on the
    bot token.
    """
    assert channel_token("C9", bot_token="xoxb", user_token="xoxp", is_ext_shared=None, op=SlackOp.READ) == "xoxb"


def test_single_credential_unchanged() -> None:
    """The single-credential short-circuits are bit-for-bit unchanged.

    No user token -> bot regardless of op / membership; no bot token ->
    user regardless of op / membership.
    """
    assert channel_token("C9", bot_token="xoxb", user_token="", is_ext_shared=None, op=SlackOp.WRITE) == "xoxb"
    assert channel_token("C9", bot_token="", user_token="xoxp", is_ext_shared=None, op=SlackOp.READ) == "xoxp"
