"""Tests for ``teatree.cli.slack_dm_provisioning`` — first-run IM provisioning (#1342).

The per-overlay bot needs an IM channel id cached in ``~/.teatree.toml`` so
that DMs route through the overlay's own bot rather than silently falling
back to whichever bot already has an IM open with the user. The provisioner
is invoked by ``t3 setup`` and surfaces clean errors at setup time rather
than at first DM attempt mid-run.
"""

from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
import tomlkit

from teatree.backends.slack.bot import SlackBotBackend
from teatree.cli import slack_dm_provisioning
from teatree.cli.slack_dm_provisioning import ProvisionResult, provision_overlay_dm_channel, resolve_user_slack_id


class TestResolveUserSlackId:
    def test_returns_pass_value_when_available(self) -> None:
        with patch("teatree.cli.slack_dm_provisioning.read_pass", return_value="U_FROM_PASS"):
            assert resolve_user_slack_id(bot_token="xoxb-tok") == "U_FROM_PASS"

    def test_falls_back_to_auth_test_when_pass_empty(self) -> None:
        backend = MagicMock(spec=SlackBotBackend)
        backend.auth_test.return_value = {"ok": True, "user_id": "U_FROM_AUTH"}
        with (
            patch("teatree.cli.slack_dm_provisioning.read_pass", return_value=""),
            patch("teatree.cli.slack_dm_provisioning.SlackBotBackend", return_value=backend),
        ):
            assert resolve_user_slack_id(bot_token="xoxb-tok") == "U_FROM_AUTH"

    def test_returns_empty_when_auth_test_fails(self) -> None:
        backend = MagicMock(spec=SlackBotBackend)
        backend.auth_test.return_value = {"ok": False, "error": "invalid_auth"}
        with (
            patch("teatree.cli.slack_dm_provisioning.read_pass", return_value=""),
            patch("teatree.cli.slack_dm_provisioning.SlackBotBackend", return_value=backend),
        ):
            assert resolve_user_slack_id(bot_token="xoxb-tok") == ""


class TestProvisionOverlayDmChannel:
    def _write_config(self, path: Path, body: str) -> None:
        path.write_text(body, encoding="utf-8")

    def test_skips_overlay_without_slack_bot(self, tmp_path: Path) -> None:
        config = tmp_path / "teatree.toml"
        self._write_config(config, '[overlays.teatree]\npath = "/repo"\n')
        result = provision_overlay_dm_channel(config_path=config, overlay_name="teatree")
        assert result.status == ProvisionResult.SKIPPED_NO_BOT

    def test_skips_when_already_provisioned(self, tmp_path: Path) -> None:
        config = tmp_path / "teatree.toml"
        self._write_config(
            config,
            "[overlays.teatree]\n"
            'messaging_backend = "slack"\n'
            'slack_token_ref = "teatree/teatree/slack"\n'
            'slack_user_id = "U01ABCD1234"\n'
            'slack_dm_channel_id = "D0CACHED"\n',
        )
        result = provision_overlay_dm_channel(config_path=config, overlay_name="teatree")
        assert result.status == ProvisionResult.SKIPPED_ALREADY_PROVISIONED
        assert result.channel_id == "D0CACHED"

    def test_opens_dm_and_persists_channel_id(self, tmp_path: Path) -> None:
        config = tmp_path / "teatree.toml"
        self._write_config(
            config,
            "[overlays.teatree]\n"
            'messaging_backend = "slack"\n'
            'slack_token_ref = "teatree/teatree/slack"\n'
            'slack_user_id = "U01ABCD1234"\n',
        )

        backend = MagicMock(spec=SlackBotBackend)
        backend.open_dm.return_value = "D0DEMOCLNT1"

        with (
            patch(
                "teatree.cli.slack_dm_provisioning.read_pass",
                side_effect=lambda key: "xoxb-tok" if key.endswith("-bot") else "",
            ),
            patch("teatree.cli.slack_dm_provisioning.SlackBotBackend", return_value=backend),
        ):
            result = provision_overlay_dm_channel(config_path=config, overlay_name="teatree")

        backend.open_dm.assert_called_once_with("U01ABCD1234")
        assert result.status == ProvisionResult.PROVISIONED
        assert result.channel_id == "D0DEMOCLNT1"

        document = cast("dict[str, Any]", tomlkit.parse(config.read_text(encoding="utf-8")))
        assert document["overlays"]["teatree"]["slack_dm_channel_id"] == "D0DEMOCLNT1"

    def test_fails_when_conversations_open_returns_empty(self, tmp_path: Path) -> None:
        config = tmp_path / "teatree.toml"
        self._write_config(
            config,
            "[overlays.teatree]\n"
            'messaging_backend = "slack"\n'
            'slack_token_ref = "teatree/teatree/slack"\n'
            'slack_user_id = "U01ABCD1234"\n',
        )

        backend = MagicMock(spec=SlackBotBackend)
        backend.open_dm.return_value = ""

        with (
            patch(
                "teatree.cli.slack_dm_provisioning.read_pass",
                side_effect=lambda key: "xoxb-tok" if key.endswith("-bot") else "",
            ),
            patch("teatree.cli.slack_dm_provisioning.SlackBotBackend", return_value=backend),
        ):
            result = provision_overlay_dm_channel(config_path=config, overlay_name="teatree")

        assert result.status == ProvisionResult.FAILED_OPEN_DM
        document = cast("dict[str, Any]", tomlkit.parse(config.read_text(encoding="utf-8")))
        assert "slack_dm_channel_id" not in document["overlays"]["teatree"]

    def test_skips_when_no_bot_token_in_pass(self, tmp_path: Path) -> None:
        config = tmp_path / "teatree.toml"
        self._write_config(
            config,
            "[overlays.teatree]\n"
            'messaging_backend = "slack"\n'
            'slack_token_ref = "teatree/teatree/slack"\n'
            'slack_user_id = "U01ABCD1234"\n',
        )

        with patch("teatree.cli.slack_dm_provisioning.read_pass", return_value=""):
            result = provision_overlay_dm_channel(config_path=config, overlay_name="teatree")

        assert result.status == ProvisionResult.SKIPPED_NO_BOT_TOKEN

    def test_uses_pass_slack_user_id_when_overlay_has_none(self, tmp_path: Path) -> None:
        """Setup falls back to ``pass slack/user-id`` when the overlay has no ``slack_user_id`` yet."""
        config = tmp_path / "teatree.toml"
        self._write_config(
            config,
            '[overlays.teatree]\nmessaging_backend = "slack"\nslack_token_ref = "teatree/teatree/slack"\n',
        )

        backend = MagicMock(spec=SlackBotBackend)
        backend.open_dm.return_value = "D0DEMOCLNT1"

        def fake_read_pass(key: str) -> str:
            if key.endswith("-bot"):
                return "xoxb-tok"
            if key == slack_dm_provisioning.SLACK_USER_ID_PASS_KEY:
                return "U_FROM_PASS"
            return ""

        with (
            patch("teatree.cli.slack_dm_provisioning.read_pass", side_effect=fake_read_pass),
            patch("teatree.cli.slack_dm_provisioning.SlackBotBackend", return_value=backend),
        ):
            result = provision_overlay_dm_channel(config_path=config, overlay_name="teatree")

        backend.open_dm.assert_called_once_with("U_FROM_PASS")
        assert result.status == ProvisionResult.PROVISIONED
        assert result.channel_id == "D0DEMOCLNT1"


class TestPersistedChannelKey:
    """The cached channel id MUST live under ``[overlays.<name>] slack_dm_channel_id``."""

    def test_canonical_toml_key_name(self) -> None:
        assert slack_dm_provisioning.SLACK_DM_CHANNEL_TOML_KEY == "slack_dm_channel_id"

    def test_canonical_pass_user_id_key(self) -> None:
        assert slack_dm_provisioning.SLACK_USER_ID_PASS_KEY == "slack/user-id"


class TestSlackBackendUsesCachedChannel:
    """``SlackBotBackend`` must short-circuit ``open_dm`` when a cached channel id is set.

    This is the consumer side of #1342: even when the per-overlay bot has
    been freshly created and never had an IM with the user, every DM-sending
    path (``notify_user``, ``DailyDigest``, ``review_nag``) reads the cached
    channel id without re-calling ``conversations.open``.
    """

    def test_open_dm_returns_cached_channel_id_for_configured_user(self) -> None:
        backend = SlackBotBackend(
            bot_token="xoxb-test",
            user_id="U01ABCD1234",
            dm_channel_id="D0DEMOCLNT1",
        )
        # Reading the cached channel must not perform any HTTP request.
        with patch.object(backend, "_post") as post:
            channel = backend.open_dm("U01ABCD1234")
        post.assert_not_called()
        assert channel == "D0DEMOCLNT1"

    def test_open_dm_falls_back_to_live_call_for_unconfigured_user(self) -> None:
        backend = SlackBotBackend(
            bot_token="xoxb-test",
            user_id="U01ABCD1234",
            dm_channel_id="D0CACHED",
        )
        with patch.object(
            backend,
            "_post",
            return_value={"ok": True, "channel": {"id": "D_OTHER"}},
        ) as post:
            channel = backend.open_dm("U_DIFFERENT")
        post.assert_called_once()
        assert channel == "D_OTHER"

    def test_open_dm_falls_back_when_no_cache_configured(self) -> None:
        """Pre-#1342 callers without a cache still hit ``conversations.open``."""
        backend = SlackBotBackend(bot_token="xoxb-test", user_id="U01ABCD1234")
        with patch.object(
            backend,
            "_post",
            return_value={"ok": True, "channel": {"id": "D_LIVE"}},
        ) as post:
            channel = backend.open_dm("U01ABCD1234")
        post.assert_called_once()
        assert channel == "D_LIVE"


class TestMessagingFromTomlThreadsCachedChannel:
    """The TOML-only fallback must read ``slack_dm_channel_id`` and thread it into the backend.

    Pre-fix: ``messaging_from_overlay('teatree')`` returns a ``SlackBotBackend``
    that has never opened an IM with the user. The first DM through it
    re-derives the channel via ``conversations.open``, which fails for a
    fresh per-overlay bot. The fix threads the cached channel into the
    backend so the DM lands on the right bot's IM.
    """

    def test_messaging_from_toml_threads_dm_channel_id_into_backend(self) -> None:
        from teatree.core import backend_factory  # noqa: PLC0415

        cfg = {
            "messaging_backend": "slack",
            "slack_token_ref": "teatree/teatree/slack",
            "slack_user_id": "U01ABCD1234",
            "slack_dm_channel_id": "D0DEMOCLNT1",
        }
        pass_lookups = {
            "teatree/teatree/slack-bot": "xoxb-bot-tok",
            "teatree/teatree/slack-app": "xapp-app-tok",
        }
        with patch("teatree.utils.secrets.read_pass", side_effect=lambda k: pass_lookups.get(k, "")):
            backend = backend_factory._messaging_from_toml(cfg)
        assert isinstance(backend, SlackBotBackend)
        with patch.object(backend, "_post") as post:
            assert backend.open_dm("U01ABCD1234") == "D0DEMOCLNT1"
        post.assert_not_called()


class TestProvisionAllOverlayDmChannels:
    """Setup-time iterator over every Slack-bot overlay block in the TOML file."""

    def test_iterates_every_slack_overlay_and_skips_non_slack(self, tmp_path: Path) -> None:
        config = tmp_path / "teatree.toml"
        config.write_text(
            "[overlays.teatree]\n"
            'messaging_backend = "slack"\n'
            'slack_token_ref = "teatree/teatree/slack"\n'
            'slack_user_id = "U01ABCD1234"\n'
            "\n"
            "[overlays.acme]\n"
            'messaging_backend = "slack"\n'
            'slack_token_ref = "teatree/acme/slack"\n'
            'slack_user_id = "U01ABCD1234"\n'
            'slack_dm_channel_id = "D0CACHED"\n'
            "\n"
            "[overlays.other]\n"
            'path = "/repo"\n',
            encoding="utf-8",
        )

        backend = MagicMock(spec=SlackBotBackend)
        backend.open_dm.return_value = "D0NEW"

        echo_lines: list[str] = []

        def fake_read_pass(key: str) -> str:
            if key.endswith("-bot"):
                return "xoxb-tok"
            return ""

        with (
            patch("teatree.cli.slack_dm_provisioning.read_pass", side_effect=fake_read_pass),
            patch("teatree.cli.slack_dm_provisioning.SlackBotBackend", return_value=backend),
        ):
            results = slack_dm_provisioning.provision_all_overlay_dm_channels(
                config_path=config,
                echo=echo_lines.append,
            )

        statuses = {r.overlay_name: r.status for r in results}
        assert statuses == {
            "teatree": ProvisionResult.PROVISIONED,
            "acme": ProvisionResult.SKIPPED_ALREADY_PROVISIONED,
        }
        assert any("Provisioned Slack IM for overlay `teatree`" in line for line in echo_lines)
        assert any("already provisioned" in line for line in echo_lines)


if __name__ == "__main__":
    pytest.main([__file__, "-x", "-q"])
