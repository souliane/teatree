"""The single Slack token-selection policy (#1084).

Extracted verbatim from ``SlackBotBackend._channel_token`` so the
review-request dedup guard reads channel history with the same token the
post will use (read-token == post-token). These assert each branch of
the policy directly; ``test_slack_connect_channel_routing.py`` proves the
``SlackBotBackend`` refactor stays behaviour-preserving.
"""

from teatree.backends.slack_token_policy import channel_token


def test_no_user_token_returns_bot_token() -> None:
    assert channel_token("C1", bot_token="xoxb", user_token="", is_ext_shared=True) == "xoxb"


def test_no_bot_token_returns_user_token() -> None:
    assert channel_token("C1", bot_token="", user_token="xoxp", is_ext_shared=False) == "xoxp"


def test_dm_channel_returns_bot_token() -> None:
    assert channel_token("D123", bot_token="xoxb", user_token="xoxp", is_ext_shared=True) == "xoxb"


def test_ext_shared_channel_returns_user_token() -> None:
    assert channel_token("C9", bot_token="xoxb", user_token="xoxp", is_ext_shared=True) == "xoxp"


def test_internal_channel_returns_bot_token() -> None:
    assert channel_token("C9", bot_token="xoxb", user_token="xoxp", is_ext_shared=False) == "xoxb"
