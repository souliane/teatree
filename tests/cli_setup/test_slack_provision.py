"""Tests for ``t3 setup slack-provision`` — full Slack lifecycle (#1686)."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
import typer
from typer.testing import CliRunner

from teatree.cli.setup import setup_app
from teatree.cli.slack_channel_provisioning import ChannelJoinResult, JoinStatus
from teatree.cli.slack_dm_provisioning import ProvisionResult
from teatree.cli.slack_provision import (
    OverlayProvisionReport,
    _broadcast_channels,
    _provision_channels,
    _push_manifest,
    _render_dm,
    _resolve_app_id,
    _slack_overlays,
    _verify_user_token,
    manifest_json,
    provision_overlay,
)
from teatree.cli.slack_setup import SlackManifestError
from teatree.cli.slack_user_token_setup import REQUIRED_USER_SCOPES
from teatree.config import OverlayEntry


def _config_with_overlay(tmp_path: Path) -> Path:
    config = tmp_path / "teatree.toml"
    config.write_text(
        "[overlays.t3]\n"
        'messaging_backend = "slack"\n'
        'slack_token_ref = "teatree/t3/slack"\n'
        'slack_app_id = "A_T3"\n'
        'slack_user_id = "U1"\n',
        encoding="utf-8",
    )
    return config


class TestProvisionOverlay:
    def test_runs_full_lifecycle_and_reports(self, tmp_path: Path) -> None:
        config = _config_with_overlay(tmp_path)
        lines: list[str] = []
        join_results = [ChannelJoinResult(status=JoinStatus.JOINED, channel_name="rev", channel_id="C1")]
        dm = ProvisionResult(status=ProvisionResult.PROVISIONED, overlay_name="t3", channel_id="D1")
        with (
            patch("teatree.cli.slack_provision._push_manifest", return_value="updated") as push,
            patch("teatree.cli.slack_provision._provision_channels", return_value=join_results),
            patch("teatree.cli.slack_provision.provision_overlay_dm_channel", return_value=dm),
            patch("teatree.cli.slack_provision.webbrowser.open") as browser,
        ):
            report = provision_overlay(config_path=config, overlay="t3", echo=lines.append, open_browser=True)
        assert report.app_id == "A_T3"
        assert report.manifest_action == "updated"
        assert report.channel_results == join_results
        assert report.dm_result is dm
        push.assert_called_once()
        browser.assert_called_once()
        assert any("install" in line.lower() for line in lines)

    def test_prints_exact_install_url(self, tmp_path: Path) -> None:
        config = _config_with_overlay(tmp_path)
        lines: list[str] = []
        with (
            patch("teatree.cli.slack_provision._push_manifest", return_value="current"),
            patch("teatree.cli.slack_provision._provision_channels", return_value=[]),
            patch(
                "teatree.cli.slack_provision.provision_overlay_dm_channel",
                return_value=ProvisionResult(status=ProvisionResult.SKIPPED_ALREADY_PROVISIONED, channel_id="D1"),
            ),
            patch("teatree.cli.slack_provision.webbrowser.open"),
        ):
            report = provision_overlay(config_path=config, overlay="t3", echo=lines.append, open_browser=False)
        assert report.install_url == "https://api.slack.com/apps/A_T3/install-on-team"
        assert any("https://api.slack.com/apps/A_T3/install-on-team" in line for line in lines)

    def test_manifest_error_is_captured_not_fatal(self, tmp_path: Path) -> None:
        config = _config_with_overlay(tmp_path)
        lines: list[str] = []
        with (
            patch("teatree.cli.slack_provision._push_manifest", side_effect=SlackManifestError("boom")),
            patch("teatree.cli.slack_provision._provision_channels", return_value=[]),
            patch(
                "teatree.cli.slack_provision.provision_overlay_dm_channel",
                return_value=ProvisionResult(status=ProvisionResult.SKIPPED_ALREADY_PROVISIONED, channel_id="D1"),
            ),
            patch("teatree.cli.slack_provision.webbrowser.open"),
        ):
            report = provision_overlay(config_path=config, overlay="t3", echo=lines.append, open_browser=False)
        assert report.manifest_action == "error"
        assert any("boom" in note for note in report.notes)


class TestManifestJson:
    def test_includes_reactions_write_in_user_scopes(self) -> None:
        body = manifest_json("t3")
        assert "reactions:write" in body


class TestSlackProvisionCommand:
    def _run(self, args: list[str]) -> object:
        return CliRunner().invoke(setup_app, args)

    def test_rejects_unregistered_overlay(self, tmp_path: Path) -> None:
        config = _config_with_overlay(tmp_path)
        with patch("teatree.cli.slack_provision.discover_overlays", return_value=[]):
            result = self._run(["slack-provision", "--overlay", "nope", "--config", str(config)])
        assert result.exit_code == 1
        assert "not registered" in result.stdout

    def test_no_slack_overlays_exits(self, tmp_path: Path) -> None:
        config = tmp_path / "teatree.toml"
        config.write_text('[overlays.t3]\npath = "/repo"\n', encoding="utf-8")
        result = self._run(["slack-provision", "--config", str(config)])
        assert result.exit_code == 1
        assert "No Slack-backed overlays" in result.stdout

    def test_all_overlays_runs_each_and_verifies_user_token(self, tmp_path: Path) -> None:
        config = tmp_path / "teatree.toml"
        config.write_text(
            "[overlays.t3]\n"
            'messaging_backend = "slack"\n'
            'slack_token_ref = "teatree/t3/slack"\n'
            'slack_app_id = "A_T3"\n'
            "[overlays.secondary]\n"
            'messaging_backend = "slack"\n'
            'slack_token_ref = "teatree/secondary/slack"\n'
            'slack_app_id = "A_SEC"\n',
            encoding="utf-8",
        )
        report_t3 = OverlayProvisionReport(overlay_name="t3", app_id="A_T3", manifest_action="current")
        report_sec = OverlayProvisionReport(overlay_name="secondary", app_id="A_SEC", manifest_action="current")
        with (
            patch("teatree.cli.slack_provision.provision_overlay", side_effect=[report_t3, report_sec]) as prov,
            patch("teatree.cli.slack_provision._verify_user_token") as verify,
        ):
            result = self._run(["slack-provision", "--config", str(config), "--no-open-browser"])
        assert result.exit_code == 0
        assert prov.call_count == 2
        verify.assert_called_once()
        assert "t3: app A_T3" in result.stdout
        assert "secondary: app A_SEC" in result.stdout


class TestVerifyUserToken:
    def test_reports_missing_reactions_write(self) -> None:
        lines: list[str] = []
        with (
            patch("teatree.cli.slack_provision.read_pass", return_value="xoxp-tok"),
            patch("teatree.cli.slack_user_token_setup.fetch_token_scopes", return_value=["chat:write"]),
        ):
            _verify_user_token(lines.append)
        assert any("reactions:write" in line for line in lines)

    def test_reports_all_present(self) -> None:
        lines: list[str] = []
        with (
            patch("teatree.cli.slack_provision.read_pass", return_value="xoxp-tok"),
            patch("teatree.cli.slack_user_token_setup.fetch_token_scopes", return_value=list(REQUIRED_USER_SCOPES)),
        ):
            _verify_user_token(lines.append)
        assert any("every required scope" in line for line in lines)

    def test_no_token_prompts_user_token_command(self) -> None:
        lines: list[str] = []
        with patch("teatree.cli.slack_provision.read_pass", return_value=""):
            _verify_user_token(lines.append)
        assert any("slack-user-token" in line for line in lines)


class TestProvisionChannels:
    def test_joins_overlay_review_channels(self, tmp_path: Path) -> None:
        config = _config_with_overlay(tmp_path)
        lines: list[str] = []
        backend = MagicMock()
        with (
            patch("teatree.cli.slack_provision._broadcast_channels", return_value=[("rev", "C1")]),
            patch("teatree.cli.slack_provision.read_pass", return_value="xoxb-bot"),
            patch("teatree.backends.slack.bot.SlackBotBackend", return_value=backend),
            patch(
                "teatree.cli.slack_provision.join_review_channels",
                return_value=[ChannelJoinResult(status=JoinStatus.JOINED, channel_name="rev", channel_id="C1")],
            ),
        ):
            results = _provision_channels(config_path=config, overlay="t3", echo=lines.append)
        assert len(results) == 1
        assert results[0].channel_id == "C1"

    def test_no_channels_returns_empty(self, tmp_path: Path) -> None:
        config = _config_with_overlay(tmp_path)
        with patch("teatree.cli.slack_provision._broadcast_channels", return_value=[]):
            assert _provision_channels(config_path=config, overlay="t3", echo=lambda _: None) == []

    def test_no_bot_token_skips(self, tmp_path: Path) -> None:
        config = _config_with_overlay(tmp_path)
        lines: list[str] = []
        with (
            patch("teatree.cli.slack_provision._broadcast_channels", return_value=[("rev", "C1")]),
            patch("teatree.cli.slack_provision.read_pass", return_value=""),
        ):
            assert _provision_channels(config_path=config, overlay="t3", echo=lines.append) == []
        assert any("No bot token" in line for line in lines)


class TestResolveAppId:
    def test_config_app_id_used(self, tmp_path: Path) -> None:
        config = _config_with_overlay(tmp_path)
        assert _resolve_app_id(config_path=config, overlay="t3", echo=lambda _: None) == "A_T3"

    def test_prompts_and_persists_when_unresolvable(self, tmp_path: Path) -> None:
        config = tmp_path / "teatree.toml"
        config.write_text('[overlays.t3]\nmessaging_backend = "slack"\n', encoding="utf-8")
        with (
            patch("teatree.cli.slack_provision.resolve_overlay_app_id", return_value=""),
            patch("teatree.cli.slack_provision.typer.prompt", return_value="A0TYPED99"),
        ):
            assert _resolve_app_id(config_path=config, overlay="t3", echo=lambda _: None) == "A0TYPED99"
        assert 'slack_app_id = "A0TYPED99"' in config.read_text(encoding="utf-8")

    def test_invalid_prompt_exits(self, tmp_path: Path) -> None:
        config = _config_with_overlay(tmp_path)
        with (
            patch("teatree.cli.slack_provision.resolve_overlay_app_id", return_value=""),
            patch("teatree.cli.slack_provision.typer.prompt", return_value="not-an-app-id"),
            pytest.raises(typer.Exit),
        ):
            _resolve_app_id(config_path=config, overlay="t3", echo=lambda _: None)


class TestPushManifest:
    def test_degraded_without_config_token(self) -> None:
        lines: list[str] = []
        with patch("teatree.cli.slack_provision.read_pass", return_value=""):
            assert _push_manifest(overlay="t3", app_id="A1", echo=lines.append) == "degraded"
        assert any("app-config token" in line for line in lines)

    def test_degraded_warns_loudly_that_user_scopes_are_not_set(self) -> None:
        lines: list[str] = []
        with patch("teatree.cli.slack_provision.read_pass", return_value=""):
            _push_manifest(overlay="t3", app_id="A1", echo=lines.append)
        joined = "\n".join(lines)
        # The degraded path must NOT read as a success: it states the manifest
        # was not pushed, that user scopes are unset, and lists the scopes to
        # add manually so the user can fix the zero-user-scope app.
        assert "DEGRADED" in joined
        assert "manifest NOT pushed" in joined
        assert "reactions:write" in joined

    def test_current_when_equivalent(self) -> None:
        with (
            patch("teatree.cli.slack_provision.read_pass", return_value="cfg-tok"),
            patch("teatree.cli.slack_provision._export_with_rotation", return_value={"x": 1}),
            patch("teatree.cli.slack_provision.build_manifest", return_value={"x": 1}),
            patch("teatree.cli.slack_provision.manifests_equivalent", return_value=True),
        ):
            assert _push_manifest(overlay="t3", app_id="A1", echo=lambda _: None) == "current"

    def test_updated_when_changed(self) -> None:
        with (
            patch("teatree.cli.slack_provision.read_pass", return_value="cfg-tok"),
            patch("teatree.cli.slack_provision._export_with_rotation", return_value={}),
            patch("teatree.cli.slack_provision.build_manifest", return_value={"y": 2}),
            patch("teatree.cli.slack_provision.manifests_equivalent", return_value=False),
            patch("teatree.cli.slack_provision.update_manifest", return_value={"permissions_updated": True}),
        ):
            assert _push_manifest(overlay="t3", app_id="A1", echo=lambda _: None) == "updated"


class TestBroadcastChannels:
    def test_returns_channels_from_overlay(self) -> None:
        overlay_obj = MagicMock()
        overlay_obj.config.get_review_broadcast_channels.return_value = [("rev", "C1")]
        with patch("teatree.cli.slack_provision.get_overlay", return_value=overlay_obj):
            assert _broadcast_channels("t3") == [("rev", "C1")]

    def test_unregistered_overlay_returns_empty(self) -> None:
        with patch("teatree.cli.slack_provision.get_overlay", side_effect=RuntimeError("nope")):
            assert _broadcast_channels("t3") == []


class TestRenderDm:
    def test_all_status_branches_emit_a_line(self) -> None:
        for status in (
            ProvisionResult.PROVISIONED,
            ProvisionResult.SKIPPED_ALREADY_PROVISIONED,
            ProvisionResult.SKIPPED_NO_BOT_TOKEN,
            ProvisionResult.SKIPPED_NO_USER_ID,
            ProvisionResult.FAILED_OPEN_DM,
        ):
            lines: list[str] = []
            _render_dm(ProvisionResult(status=status, channel_id="D1", detail="why"), lines.append)
            assert lines


class TestVerifyUserTokenError:
    def test_http_error_warns(self) -> None:
        lines: list[str] = []
        with (
            patch("teatree.cli.slack_provision.read_pass", return_value="xoxp-tok"),
            patch(
                "teatree.cli.slack_user_token_setup.fetch_token_scopes",
                side_effect=httpx.HTTPError("net down"),
            ),
        ):
            _verify_user_token(lines.append)
        assert any("Could not verify" in line for line in lines)


class TestSlackOverlaysHelper:
    def test_lists_only_slack_overlays(self, tmp_path: Path) -> None:
        config = tmp_path / "teatree.toml"
        config.write_text(
            '[overlays.t3]\nmessaging_backend = "slack"\n[overlays.other]\npath = "/x"\n',
            encoding="utf-8",
        )
        assert _slack_overlays(config) == ["t3"]

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert _slack_overlays(tmp_path / "nope.toml") == []


class TestCommandSingleOverlay:
    def test_provisions_named_overlay(self, tmp_path: Path) -> None:
        config = _config_with_overlay(tmp_path)
        report = OverlayProvisionReport(overlay_name="t3", app_id="A_T3", manifest_action="current")
        with (
            patch(
                "teatree.cli.slack_provision.discover_overlays",
                return_value=[OverlayEntry(name="t3", overlay_class="x:Y")],
            ),
            patch("teatree.cli.slack_provision.provision_overlay", return_value=report) as prov,
            patch("teatree.cli.slack_provision._verify_user_token"),
        ):
            result = CliRunner().invoke(
                setup_app,
                ["slack-provision", "--overlay", "t3", "--config", str(config), "--no-open-browser"],
            )
        assert result.exit_code == 0
        prov.assert_called_once()
        assert "t3: app A_T3" in result.stdout
