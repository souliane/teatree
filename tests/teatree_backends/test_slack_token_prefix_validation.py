"""Token-prefix validation at backend construction (#1285, codex #1282 item 5).

``SlackBotBackend.__init__`` must reject a token whose prefix does not
match its slot:

- ``bot_token`` must start with ``xoxb-``
- ``user_token`` must start with ``xoxp-`` (only when non-empty)
- ``app_token`` must start with ``xapp-`` (only when non-empty)

The capture-time check in ``slack_user_token_setup._prompt_user_token`` is
not sufficient — the token sits in pass as a raw string and the runtime
construction path (``backend_factory._messaging_from_toml``,
``backends.loader.get_messaging``) never re-validates. A swapped or
pasted-wrong token reaches the deterministic policy in
``slack_token_policy.channel_token()`` which routes by *slot*, never by
prefix; the post then either impersonates the user or is silently dropped
on a Slack-Connect channel.

The runtime gate fires at backend construction (``__init__``) so the
loop fails *loudly but early* — a clear ``TokenSlotMismatchError`` with
the offending slot named and the right ``t3 setup …`` command pointed
at, never a mid-tick crash. Empty tokens stay valid (the legacy
single-credential / no-credential cases).
"""

from unittest.mock import patch

import pytest

from teatree.backends.slack_bot import SlackBotBackend
from teatree.backends.slack_token_validation import TokenSlotMismatchError
from teatree.core import backend_factory


class TestSlackBotBackendConstructionRejectsSwappedTokens:
    """``SlackBotBackend(...)`` is the single chokepoint every factory path hits."""

    def test_bot_slot_rejects_xoxp_user_token(self) -> None:
        with pytest.raises(TokenSlotMismatchError) as excinfo:
            SlackBotBackend(bot_token="xoxp-pasted-into-bot-slot")
        message = str(excinfo.value)
        assert "bot_token" in message
        assert "xoxb-" in message
        assert "t3 setup slack-bot" in message

    def test_user_slot_rejects_xoxb_bot_token(self) -> None:
        """The exact failure mode observed in the field on 2026-05-20."""
        with pytest.raises(TokenSlotMismatchError) as excinfo:
            SlackBotBackend(bot_token="xoxb-real-bot", user_token="xoxb-pasted-into-user-slot")
        message = str(excinfo.value)
        assert "user_token" in message
        assert "xoxp-" in message
        assert "t3 setup slack-user-token" in message

    def test_app_slot_rejects_xoxb_in_app_token(self) -> None:
        with pytest.raises(TokenSlotMismatchError) as excinfo:
            SlackBotBackend(bot_token="xoxb-real-bot", app_token="xoxb-not-an-app-token")
        message = str(excinfo.value)
        assert "app_token" in message
        assert "xapp-" in message
        assert "t3 setup slack-bot" in message

    def test_bot_slot_rejects_unrecognised_prefix(self) -> None:
        """A typo'd token (no recognised Slack prefix) fails just as loud."""
        with pytest.raises(TokenSlotMismatchError):
            SlackBotBackend(bot_token="not-a-slack-token")

    def test_well_formed_tokens_all_three_slots_accepted(self) -> None:
        """Happy path — every slot carries the right prefix, construction succeeds."""
        backend = SlackBotBackend(
            bot_token="xoxb-real-bot-token",
            app_token="xapp-real-app-token",
            user_token="xoxp-real-user-token",
            user_id="U123",
        )
        assert backend.user_token == "xoxp-real-user-token"
        assert backend.app_token == "xapp-real-app-token"

    def test_empty_bot_token_still_allowed(self) -> None:
        """Empty bot_token is the no-credential case — must stay accepted."""
        SlackBotBackend(bot_token="")

    def test_empty_user_token_still_allowed(self) -> None:
        """Empty user_token is the bot-only deployment — must stay accepted."""
        SlackBotBackend(bot_token="xoxb-real-bot")

    def test_empty_app_token_still_allowed(self) -> None:
        """Empty app_token is the no-socket-mode case — must stay accepted."""
        SlackBotBackend(bot_token="xoxb-real-bot", app_token="")


class TestFactoryConstructionFailsLoudOnPassSwap:
    """The runtime path the codex finding flagged: a swapped token in ``pass``."""

    def test_messaging_from_toml_swapped_user_slot_raises(self) -> None:
        """``pass slack/user-oauth-token`` holds an ``xoxb-…`` token (the field bug)."""
        cfg = {
            "messaging_backend": "slack",
            "slack_token_ref": "ref",
            "user_token_ref": "slack/user-oauth",
            "slack_user_id": "U1",
        }
        # The exact field failure mode: an xoxb-… ended up at the xoxp slot.
        pass_lookups = {
            "ref-bot": "xoxb-real-bot",
            "ref-app": "xapp-real-app",
            "slack/user-oauth": "xoxb-mistakenly-pasted-here",
        }
        with (
            patch("teatree.utils.secrets.read_pass", side_effect=lambda k: pass_lookups.get(k, "")),
            pytest.raises(TokenSlotMismatchError),
        ):
            backend_factory._messaging_from_toml(cfg)

    def test_messaging_from_toml_swapped_bot_slot_raises(self) -> None:
        """The mirror case: ``pass slack/ref-bot`` holds an ``xoxp-…`` token."""
        cfg = {"messaging_backend": "slack", "slack_token_ref": "ref"}
        pass_lookups = {"ref-bot": "xoxp-pasted-into-bot-slot", "ref-app": "xapp-real-app"}
        with (
            patch("teatree.utils.secrets.read_pass", side_effect=lambda k: pass_lookups.get(k, "")),
            pytest.raises(TokenSlotMismatchError),
        ):
            backend_factory._messaging_from_toml(cfg)
