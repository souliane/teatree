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

The runtime gate fires at backend construction (``__init__``). The *bot*
and *app* slots stay strict everywhere — a wrong token there is a genuine
misconfiguration that must surface loudly with a clear
``TokenSlotMismatchError``. The *user* slot is optional capability
(colleague-channel posts/reactions under the human identity): the loop
construction paths degrade a malformed user token to bot-only with a
one-time WARNING (``degrade_bad_user_token=True``) so a Slack-only typo
never wedges merges, CI, or PR sweeps; the explicit setup/provision path
keeps :func:`assert_user_token` so a wrong paste there still errors loudly.
Empty tokens stay valid (the legacy single-credential / no-credential
cases).
"""

import logging
from unittest.mock import patch

import pytest

from teatree.backends.slack.bot import SlackBotBackend
from teatree.backends.slack.token_validation import (
    TokenSlotMismatchError,
    assert_user_token,
    resolve_user_token_or_degrade,
)
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


class TestLoopConstructionDegradesBadUserToken:
    """``degrade_bad_user_token=True`` — a malformed user token must NOT crash the loop.

    A Slack-only credential typo (an ``xoxb-…`` pasted into the ``xoxp``
    slot) must never wedge merges, CI, or PR sweeps. The loop construction
    paths degrade the user token to absent (bot-only) with a warning.
    """

    def test_swapped_user_slot_degrades_to_bot_only(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            backend = SlackBotBackend(
                bot_token="xoxb-real-bot",
                user_token="xoxb-pasted-into-user-slot",
                degrade_bad_user_token=True,
            )
        assert backend.user_token == ""
        assert "t3 setup slack-user-token" in caplog.text

    def test_valid_user_token_survives_degrade_mode(self) -> None:
        backend = SlackBotBackend(
            bot_token="xoxb-real-bot",
            user_token="xoxp-real-user",
            degrade_bad_user_token=True,
        )
        assert backend.user_token == "xoxp-real-user"

    def test_bot_slot_stays_strict_even_in_degrade_mode(self) -> None:
        """Degrade only relaxes the *user* slot — a bad bot token still raises."""
        with pytest.raises(TokenSlotMismatchError):
            SlackBotBackend(bot_token="xoxp-pasted-into-bot-slot", degrade_bad_user_token=True)

    def test_app_slot_stays_strict_even_in_degrade_mode(self) -> None:
        with pytest.raises(TokenSlotMismatchError):
            SlackBotBackend(bot_token="xoxb-real-bot", app_token="xoxb-not-an-app", degrade_bad_user_token=True)


class TestResolveUserTokenOrDegrade:
    """The pure policy helper the loop paths route the user token through."""

    def test_returns_valid_token_unchanged(self) -> None:
        assert resolve_user_token_or_degrade("xoxp-good") == "xoxp-good"

    def test_returns_empty_unchanged(self) -> None:
        assert resolve_user_token_or_degrade("") == ""

    def test_degrades_mismatched_token_to_empty_with_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            assert resolve_user_token_or_degrade("xoxb-wrong-slot") == ""
        assert "t3 setup slack-user-token" in caplog.text


class TestSetupPathStaysStrict:
    """The explicit setup/provision path keeps loud validation — a wrong paste must error."""

    def test_assert_user_token_still_raises_on_mismatch(self) -> None:
        with pytest.raises(TokenSlotMismatchError):
            assert_user_token("xoxb-pasted-into-user-slot")

    def test_default_construction_stays_strict_on_user_slot(self) -> None:
        """Without the degrade flag (direct setup/provision construction) the user slot raises."""
        with pytest.raises(TokenSlotMismatchError):
            SlackBotBackend(bot_token="xoxb-real-bot", user_token="xoxb-pasted-into-user-slot")


class TestFactoryConstructionDegradesUserSlotKeepsBotStrict:
    """The runtime path the codex finding flagged — now degrades the user slot, keeps bot strict."""

    def test_messaging_from_toml_swapped_user_slot_degrades_to_bot_only(self, caplog: pytest.LogCaptureFixture) -> None:
        """``pass slack/user-oauth-token`` holds an ``xoxb-…`` token — the loop keeps ticking bot-only."""
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
            caplog.at_level(logging.WARNING),
        ):
            backend = backend_factory._messaging_from_toml(cfg)
        assert isinstance(backend, SlackBotBackend)
        assert backend.user_token == ""
        assert "t3 setup slack-user-token" in caplog.text

    def test_messaging_from_toml_swapped_bot_slot_still_raises(self) -> None:
        """The mirror case stays loud: a bad bot token is real misconfig, not optional capability."""
        cfg = {"messaging_backend": "slack", "slack_token_ref": "ref"}
        pass_lookups = {"ref-bot": "xoxp-pasted-into-bot-slot", "ref-app": "xapp-real-app"}
        with (
            patch("teatree.utils.secrets.read_pass", side_effect=lambda k: pass_lookups.get(k, "")),
            pytest.raises(TokenSlotMismatchError),
        ):
            backend_factory._messaging_from_toml(cfg)
