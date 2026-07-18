"""The unified messaging token-ref resolver + diagnosis (#3334).

``slack_token_ref`` is a prefix and ``user_token_ref`` is a full path — resolved
once here so no consumer re-derives (and mis-derives) the asymmetry.
"""

from collections.abc import Callable
from unittest.mock import patch

from teatree.core.messaging_tokens import ResolvedMessagingTokens, diagnose_configured_ref, resolve_messaging_tokens


def _store(mapping: dict[str, str]) -> Callable[[str], str]:
    return lambda key: mapping.get(key, "")


class TestResolveMessagingTokens:
    def test_prefix_and_full_path_resolve_by_different_rules(self) -> None:
        store = {
            "team/slack-bot": "xoxb-B",
            "team/slack-app": "xapp-A",
            "team/user-oauth": "xoxp-U",
        }
        with patch("teatree.utils.secrets.read_pass", side_effect=_store(store)):
            tokens = resolve_messaging_tokens(
                slack_token_ref="team/slack",
                user_token_ref="team/user-oauth",
            )
        assert tokens == ResolvedMessagingTokens(bot="xoxb-B", app="xapp-A", user="xoxp-U")

    def test_user_token_ref_is_read_verbatim_not_as_a_prefix(self) -> None:
        # A user_token_ref configured AS A PREFIX (the natural wrong guess) does
        # not resolve — proving the full-path rule is applied to it.
        store = {"team/user-oauth-bot": "wrong", "team/user-oauth": "xoxp-right"}
        with patch("teatree.utils.secrets.read_pass", side_effect=_store(store)):
            tokens = resolve_messaging_tokens(slack_token_ref="", user_token_ref="team/user-oauth")
        assert tokens.user == "xoxp-right"

    def test_unset_slack_token_ref_uses_bot_fallback(self) -> None:
        with patch("teatree.utils.secrets.read_pass", side_effect=_store({})):
            tokens = resolve_messaging_tokens(
                slack_token_ref="",
                user_token_ref="",
                bot_fallback="xoxb-fallback",
            )
        assert tokens.bot == "xoxb-fallback"
        assert tokens.app == ""


class TestDiagnoseConfiguredRef:
    def test_unset_ref_is_a_legitimate_noop(self) -> None:
        assert diagnose_configured_ref("user_token_ref", "") is None

    def test_configured_but_unresolvable_ref_is_diagnosed(self) -> None:
        with patch("teatree.utils.secrets.read_pass", side_effect=_store({})):
            msg = diagnose_configured_ref("user_token_ref", "team/user-oauth")
        assert msg is not None
        assert "user_token_ref" in msg
        assert "team/user-oauth" in msg

    def test_resolvable_ref_is_not_diagnosed(self) -> None:
        with patch("teatree.utils.secrets.read_pass", side_effect=_store({"team/user-oauth": "xoxp-U"})):
            assert diagnose_configured_ref("user_token_ref", "team/user-oauth") is None

    def test_prefix_suffix_is_probed_for_the_bot_slot(self) -> None:
        with patch("teatree.utils.secrets.read_pass", side_effect=_store({})):
            msg = diagnose_configured_ref("slack_token_ref", "team/slack", suffix="-bot")
        assert msg is not None
        assert "team/slack-bot" in msg
