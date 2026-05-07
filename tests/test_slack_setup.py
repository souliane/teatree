"""Tests for ``t3 setup slack-bot`` — interactive Slack-bot walkthrough."""

import subprocess
import sys
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest
import tomlkit
from typer.testing import CliRunner

from teatree.cli.setup import setup_app
from teatree.cli.slack_setup import (
    _BOT_TOKEN_RE,
    _USER_ID_RE,
    build_manifest,
    manifest_install_url,
    write_overlay_settings,
)
from teatree.config import OverlayEntry


class TestTomlkitImportIsLazy:
    """Regression guard: importing ``slack_setup`` must not trigger ``tomlkit``.

    A user with a stale teatree install (pre-tomlkit-dep) was unable to run
    any ``t3`` subcommand because ``cli/__init__.py`` imports ``cli/setup.py``
    which imports ``cli/slack_setup.py`` which used to ``import tomlkit`` at
    module top level. The whole CLI bootstrap crashed on the missing optional
    dep. This test runs the import in a subprocess so it sees a clean
    ``sys.modules`` and asserts ``tomlkit`` does not get pulled in.
    """

    def test_importing_slack_setup_does_not_import_tomlkit(self) -> None:
        probe = (
            "import sys\n"
            "import teatree.cli.slack_setup\n"
            "msg = 'tomlkit must stay lazy so a stale install cannot break the whole CLI'\n"
            "assert 'tomlkit' not in sys.modules, msg\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr


class TestBuildManifest:
    def test_default_display_name_uses_overlay(self) -> None:
        manifest = build_manifest(overlay_name="acme")
        assert manifest["display_information"]["name"] == "teatree-acme"

    def test_socket_mode_enabled_no_interactivity(self) -> None:
        manifest = build_manifest(overlay_name="acme")
        assert manifest["settings"]["socket_mode_enabled"] is True
        assert manifest["settings"]["interactivity"]["is_enabled"] is False

    def test_required_bot_scopes_present(self) -> None:
        manifest = build_manifest(overlay_name="acme")
        scopes = manifest["oauth_config"]["scopes"]["bot"]
        for required in ("chat:write", "im:write", "im:history", "reactions:read", "reactions:write"):
            assert required in scopes

    def test_subscribed_to_app_mention_and_dm(self) -> None:
        manifest = build_manifest(overlay_name="acme")
        events = manifest["settings"]["event_subscriptions"]["bot_events"]
        assert "app_mention" in events
        assert "message.im" in events


class TestManifestInstallUrl:
    def test_url_targets_create_app_endpoint(self) -> None:
        url = manifest_install_url(build_manifest(overlay_name="acme"))
        assert url.startswith("https://api.slack.com/apps?new_app=1&manifest_json=")

    def test_url_carries_overlay_name_in_payload(self) -> None:
        url = manifest_install_url(build_manifest(overlay_name="acme"))
        assert "teatree-acme" in url


class TestWriteOverlaySettings:
    def test_creates_overlays_block_when_missing(self, tmp_path: Path) -> None:
        config = tmp_path / "teatree.toml"
        write_overlay_settings(
            config,
            "acme",
            slack_user_id="U01ABCD1234",
            slack_bot_token_ref="teatree/acme/slack",
        )
        document = cast("dict[str, Any]", tomlkit.parse(config.read_text(encoding="utf-8")))
        assert document["overlays"]["acme"]["slack_user_id"] == "U01ABCD1234"
        assert document["overlays"]["acme"]["slack_bot_token_ref"] == "teatree/acme/slack"
        assert document["overlays"]["acme"]["messaging_backend"] == "slack"

    def test_preserves_unrelated_keys(self, tmp_path: Path) -> None:
        config = tmp_path / "teatree.toml"
        config.write_text(
            '[teatree]\nworkspace_dir = "~/work"\n\n'
            '[overlays.acme]\npath = "/p/acme"\n\n'
            '[overlays.beta]\npath = "/p/beta"\nslack_user_id = "U999"\n',
            encoding="utf-8",
        )
        write_overlay_settings(
            config,
            "acme",
            slack_user_id="U01ABCD1234",
            slack_bot_token_ref="teatree/acme/slack",
        )
        document = cast("dict[str, Any]", tomlkit.parse(config.read_text(encoding="utf-8")))
        assert document["teatree"]["workspace_dir"] == "~/work"
        assert document["overlays"]["acme"]["path"] == "/p/acme"
        assert document["overlays"]["acme"]["slack_user_id"] == "U01ABCD1234"
        assert document["overlays"]["beta"]["slack_user_id"] == "U999"
        assert document["overlays"]["beta"]["path"] == "/p/beta"

    def test_overwrites_existing_slack_settings(self, tmp_path: Path) -> None:
        config = tmp_path / "teatree.toml"
        config.write_text(
            '[overlays.acme]\nslack_user_id = "U_OLD"\nslack_bot_token_ref = "old-ref"\n',
            encoding="utf-8",
        )
        write_overlay_settings(
            config,
            "acme",
            slack_user_id="U_NEW",
            slack_bot_token_ref="new-ref",
        )
        document = cast("dict[str, Any]", tomlkit.parse(config.read_text(encoding="utf-8")))
        assert document["overlays"]["acme"]["slack_user_id"] == "U_NEW"
        assert document["overlays"]["acme"]["slack_bot_token_ref"] == "new-ref"


class TestPatterns:
    @pytest.mark.parametrize(
        "value",
        ["xoxb-1234567-abc-DEF", "xoxb-0-z"],
    )
    def test_bot_token_accepts_valid(self, value: str) -> None:
        assert _BOT_TOKEN_RE.match(value)

    @pytest.mark.parametrize(
        "value",
        ["xapp-1-A1-BC", "abc", "xoxp-1234", " xoxb-1"],
    )
    def test_bot_token_rejects_invalid(self, value: str) -> None:
        assert _BOT_TOKEN_RE.match(value) is None

    @pytest.mark.parametrize(
        "value",
        ["U01ABCD1234", "W0012345"],
    )
    def test_user_id_accepts_valid(self, value: str) -> None:
        assert _USER_ID_RE.match(value)

    @pytest.mark.parametrize(
        "value",
        ["alice", "u01abcd", "U-1234567"],
    )
    def test_user_id_rejects_invalid(self, value: str) -> None:
        assert _USER_ID_RE.match(value) is None


def _stub_overlays() -> list[OverlayEntry]:
    return [OverlayEntry(name="acme", overlay_class="acme.overlay:AcmeOverlay")]


class TestSlackBotCommand:
    def _invoke(self, tmp_path: Path, *, inputs: str, args: list[str]) -> "object":
        runner = CliRunner()
        config = tmp_path / "teatree.toml"
        return runner.invoke(
            setup_app,
            ["slack-bot", *args, "--config", str(config)],
            input=inputs,
            catch_exceptions=False,
        )

    def test_unknown_overlay_exits_with_error(self, tmp_path: Path) -> None:
        with patch("teatree.cli.slack_setup.discover_overlays", return_value=_stub_overlays()):
            result = self._invoke(tmp_path, inputs="", args=["--overlay", "ghost"])
        assert result.exit_code == 1
        assert "not registered" in result.stdout

    def test_skip_smoke_test_writes_tokens_and_settings(self, tmp_path: Path) -> None:
        config = tmp_path / "teatree.toml"
        captured: dict[str, str] = {}

        def fake_write_pass(key: str, value: str) -> bool:
            captured[key] = value
            return True

        inputs = "xoxb-1-test\nxapp-1-test\nU01ABCD1234\n"
        with (
            patch("teatree.cli.slack_setup.discover_overlays", return_value=_stub_overlays()),
            patch("teatree.cli.slack_setup.write_pass", side_effect=fake_write_pass),
            patch("teatree.cli.slack_setup.webbrowser.open"),
        ):
            result = self._invoke(
                tmp_path,
                inputs=inputs,
                args=["--overlay", "acme", "--skip-smoke-test"],
            )

        assert result.exit_code == 0, result.stdout
        assert captured["teatree/acme/slack-bot"] == "xoxb-1-test"
        assert captured["teatree/acme/slack-app"] == "xapp-1-test"
        document = cast("dict[str, Any]", tomlkit.parse(config.read_text(encoding="utf-8")))
        assert document["overlays"]["acme"]["slack_user_id"] == "U01ABCD1234"
        assert document["overlays"]["acme"]["slack_bot_token_ref"] == "teatree/acme/slack"

    def test_reset_skips_manifest_url(self, tmp_path: Path) -> None:
        opened: list[str] = []
        inputs = "xoxb-1-test\nxapp-1-test\nU01ABCD1234\n"
        with (
            patch("teatree.cli.slack_setup.discover_overlays", return_value=_stub_overlays()),
            patch("teatree.cli.slack_setup.write_pass", return_value=True),
            patch("teatree.cli.slack_setup.webbrowser.open", side_effect=opened.append),
        ):
            result = self._invoke(
                tmp_path,
                inputs=inputs,
                args=["--overlay", "acme", "--reset", "--skip-smoke-test"],
            )

        assert result.exit_code == 0, result.stdout
        assert opened == []
        assert "Reset mode" in result.stdout

    def test_pass_failure_exits_with_error(self, tmp_path: Path) -> None:
        inputs = "xoxb-1-test\nxapp-1-test\nU01ABCD1234\n"
        with (
            patch("teatree.cli.slack_setup.discover_overlays", return_value=_stub_overlays()),
            patch("teatree.cli.slack_setup.write_pass", return_value=False),
            patch("teatree.cli.slack_setup.webbrowser.open"),
        ):
            result = self._invoke(
                tmp_path,
                inputs=inputs,
                args=["--overlay", "acme", "--skip-smoke-test"],
            )

        assert result.exit_code == 1
        assert "Failed to store bot token" in result.stdout

    def test_app_token_pass_failure_exits_with_error(self, tmp_path: Path) -> None:
        inputs = "xoxb-1-test\nxapp-1-test\nU01ABCD1234\n"

        def fake_write_pass(key: str, value: str) -> bool:
            return "bot" in key  # bot succeeds, app fails

        with (
            patch("teatree.cli.slack_setup.discover_overlays", return_value=_stub_overlays()),
            patch("teatree.cli.slack_setup.write_pass", side_effect=fake_write_pass),
            patch("teatree.cli.slack_setup.webbrowser.open"),
        ):
            result = self._invoke(
                tmp_path,
                inputs=inputs,
                args=["--overlay", "acme", "--skip-smoke-test"],
            )

        assert result.exit_code == 1
        assert "Failed to store app token" in result.stdout

    def test_invalid_token_format_reprompts(self, tmp_path: Path) -> None:
        inputs = "garbage\nxoxb-1-test\nxapp-1-test\nU01ABCD1234\n"
        with (
            patch("teatree.cli.slack_setup.discover_overlays", return_value=_stub_overlays()),
            patch("teatree.cli.slack_setup.write_pass", return_value=True),
            patch("teatree.cli.slack_setup.webbrowser.open"),
        ):
            result = self._invoke(
                tmp_path,
                inputs=inputs,
                args=["--overlay", "acme", "--skip-smoke-test"],
            )

        assert result.exit_code == 0, result.stdout
        assert "Invalid bot token format" in result.stdout

    def test_invalid_user_id_format_reprompts(self, tmp_path: Path) -> None:
        inputs = "xoxb-1-test\nxapp-1-test\nbad-id\nU01ABCD1234\n"
        with (
            patch("teatree.cli.slack_setup.discover_overlays", return_value=_stub_overlays()),
            patch("teatree.cli.slack_setup.write_pass", return_value=True),
            patch("teatree.cli.slack_setup.webbrowser.open"),
        ):
            result = self._invoke(
                tmp_path,
                inputs=inputs,
                args=["--overlay", "acme", "--skip-smoke-test"],
            )

        assert result.exit_code == 0, result.stdout
        assert "Slack user ids start with" in result.stdout


class TestSmokeTest:
    """Direct tests for ``_smoke_test`` — bypasses the CLI prompts."""

    def test_open_dm_returns_empty_channel(self) -> None:
        from teatree.cli.slack_setup import _smoke_test  # noqa: PLC0415

        with patch("teatree.cli.slack_setup.SlackBotBackend") as bot_cls:
            bot_cls.return_value.open_dm.return_value = ""
            result = _smoke_test(bot_token="xoxb-1", user_id="U01ABCD1234")

        assert result is False

    def test_post_message_failure(self) -> None:
        from teatree.cli.slack_setup import _smoke_test  # noqa: PLC0415

        with patch("teatree.cli.slack_setup.SlackBotBackend") as bot_cls:
            bot_cls.return_value.open_dm.return_value = "C123"
            bot_cls.return_value.post_message.return_value = {"error": "channel_not_found"}
            result = _smoke_test(bot_token="xoxb-1", user_id="U01ABCD1234")

        assert result is False

    def test_reaction_received_within_timeout(self) -> None:
        from teatree.cli.slack_setup import _smoke_test  # noqa: PLC0415

        with (
            patch("teatree.cli.slack_setup.SlackBotBackend") as bot_cls,
            patch("teatree.cli.slack_setup.time.sleep"),
        ):
            bot_cls.return_value.open_dm.return_value = "C123"
            bot_cls.return_value.post_message.return_value = {"ts": "1.0"}
            bot_cls.return_value.get_reactions.return_value = ["white_check_mark"]
            result = _smoke_test(bot_token="xoxb-1", user_id="U01ABCD1234")

        assert result is True

    def test_reaction_timeout_returns_false(self) -> None:
        from teatree.cli.slack_setup import _smoke_test  # noqa: PLC0415

        # First monotonic call returns 0 (start), subsequent calls return values
        # past the deadline so the while loop exits without polling.
        with (
            patch("teatree.cli.slack_setup.SlackBotBackend") as bot_cls,
            patch("teatree.cli.slack_setup.time.sleep"),
            patch("teatree.cli.slack_setup.time.monotonic", side_effect=[0.0, 1.0, 9_999_999.0]),
        ):
            bot_cls.return_value.open_dm.return_value = "C123"
            bot_cls.return_value.post_message.return_value = {"ts": "1.0"}
            bot_cls.return_value.get_reactions.return_value = []
            result = _smoke_test(bot_token="xoxb-1", user_id="U01ABCD1234")

        assert result is False


class TestSmokeTestInvocation:
    def test_smoke_test_failure_exits_with_error(self, tmp_path: Path) -> None:
        inputs = "xoxb-1-test\nxapp-1-test\nU01ABCD1234\n"
        config = tmp_path / "teatree.toml"
        runner = CliRunner()
        with (
            patch("teatree.cli.slack_setup.discover_overlays", return_value=_stub_overlays()),
            patch("teatree.cli.slack_setup.write_pass", return_value=True),
            patch("teatree.cli.slack_setup.webbrowser.open"),
            patch("teatree.cli.slack_setup._smoke_test", return_value=False),
        ):
            result = runner.invoke(
                setup_app,
                ["slack-bot", "--overlay", "acme", "--config", str(config)],
                input=inputs,
                catch_exceptions=False,
            )

        assert result.exit_code == 1
