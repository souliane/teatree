"""Tests for ``teatree.cli.slack_app_resolve`` — shared app-id resolution (#1686)."""

from pathlib import Path
from unittest.mock import patch

import httpx

from teatree.cli.slack_app_resolve import (
    derive_app_id_from_token,
    persist_overlay_field,
    read_overlay_field,
    resolve_overlay_app_id,
)


class TestReadOverlayField:
    def test_returns_value_when_present(self, tmp_path: Path) -> None:
        config = tmp_path / "teatree.toml"
        config.write_text('[overlays.t3]\nslack_app_id = "A123"\n', encoding="utf-8")
        assert read_overlay_field(config, "t3", "slack_app_id") == "A123"

    def test_returns_empty_when_missing_overlay(self, tmp_path: Path) -> None:
        config = tmp_path / "teatree.toml"
        config.write_text('[overlays.other]\nslack_app_id = "A123"\n', encoding="utf-8")
        assert read_overlay_field(config, "t3", "slack_app_id") == ""

    def test_returns_empty_when_no_file(self, tmp_path: Path) -> None:
        assert read_overlay_field(tmp_path / "nope.toml", "t3", "slack_app_id") == ""


class TestPersistOverlayField:
    def test_writes_and_preserves_other_keys(self, tmp_path: Path) -> None:
        config = tmp_path / "teatree.toml"
        config.write_text('[overlays.t3]\nslack_token_ref = "teatree/t3/slack"\n', encoding="utf-8")
        persist_overlay_field(config, "t3", "slack_app_id", "A999")
        body = config.read_text(encoding="utf-8")
        assert 'slack_app_id = "A999"' in body
        assert 'slack_token_ref = "teatree/t3/slack"' in body

    def test_noop_on_empty_value(self, tmp_path: Path) -> None:
        config = tmp_path / "teatree.toml"
        config.write_text("[overlays.t3]\n", encoding="utf-8")
        persist_overlay_field(config, "t3", "slack_app_id", "")
        assert "slack_app_id" not in config.read_text(encoding="utf-8")

    def test_noop_when_overlay_block_absent(self, tmp_path: Path) -> None:
        config = tmp_path / "teatree.toml"
        config.write_text("[overlays.other]\n", encoding="utf-8")
        persist_overlay_field(config, "t3", "slack_app_id", "A999")
        assert "A999" not in config.read_text(encoding="utf-8")

    def test_noop_when_file_missing(self, tmp_path: Path) -> None:
        config = tmp_path / "nope.toml"
        persist_overlay_field(config, "t3", "slack_app_id", "A999")
        assert not config.exists()


class TestDeriveAppIdFromToken:
    def test_empty_token_returns_empty(self) -> None:
        assert derive_app_id_from_token("") == ""

    def test_derives_from_auth_and_bots_info(self) -> None:
        with patch("teatree.cli.slack_app_resolve.httpx.post") as post:
            post.side_effect = [
                _resp({"ok": True, "bot_id": "B1"}),
                _resp({"ok": True, "bot": {"app_id": "A_DERIVED"}}),
            ]
            assert derive_app_id_from_token("xoxb-tok") == "A_DERIVED"

    def test_returns_empty_when_auth_not_ok(self) -> None:
        with patch("teatree.cli.slack_app_resolve.httpx.post", return_value=_resp({"ok": False})):
            assert derive_app_id_from_token("xoxb-tok") == ""

    def test_returns_empty_when_no_bot_id(self) -> None:
        with patch("teatree.cli.slack_app_resolve.httpx.post", return_value=_resp({"ok": True})):
            assert derive_app_id_from_token("xoxb-tok") == ""

    def test_returns_empty_on_http_error(self) -> None:
        with patch("teatree.cli.slack_app_resolve.httpx.post", side_effect=httpx.HTTPError("net down")):
            assert derive_app_id_from_token("xoxb-tok") == ""

    def test_returns_empty_when_bots_info_not_ok(self) -> None:
        with patch("teatree.cli.slack_app_resolve.httpx.post") as post:
            post.side_effect = [_resp({"ok": True, "bot_id": "B1"}), _resp({"ok": False})]
            assert derive_app_id_from_token("xoxb-tok") == ""


class TestResolveOverlayAppId:
    def test_config_value_wins(self, tmp_path: Path) -> None:
        config = tmp_path / "teatree.toml"
        config.write_text('[overlays.t3]\nslack_app_id = "A_CFG"\n', encoding="utf-8")
        with patch("teatree.cli.slack_app_resolve.derive_app_id_from_token") as derive:
            assert resolve_overlay_app_id(config, "t3") == "A_CFG"
            derive.assert_not_called()

    def test_derives_and_persists_when_config_empty(self, tmp_path: Path) -> None:
        config = tmp_path / "teatree.toml"
        config.write_text('[overlays.t3]\nslack_token_ref = "teatree/t3/slack"\n', encoding="utf-8")
        with (
            patch("teatree.cli.slack_app_resolve.read_pass", return_value="xoxb-tok"),
            patch("teatree.cli.slack_app_resolve.derive_app_id_from_token", return_value="A_DERIVED"),
        ):
            assert resolve_overlay_app_id(config, "t3") == "A_DERIVED"
        assert 'slack_app_id = "A_DERIVED"' in config.read_text(encoding="utf-8")

    def test_returns_empty_when_no_token_ref(self, tmp_path: Path) -> None:
        config = tmp_path / "teatree.toml"
        config.write_text("[overlays.t3]\n", encoding="utf-8")
        assert resolve_overlay_app_id(config, "t3") == ""

    def test_uses_explicit_token_ref(self, tmp_path: Path) -> None:
        config = tmp_path / "teatree.toml"
        config.write_text("[overlays.t3]\n", encoding="utf-8")
        with (
            patch("teatree.cli.slack_app_resolve.read_pass", return_value="xoxb-tok") as read,
            patch("teatree.cli.slack_app_resolve.derive_app_id_from_token", return_value="A_X"),
        ):
            assert resolve_overlay_app_id(config, "t3", token_ref="teatree/explicit/slack") == "A_X"
            read.assert_called_once_with("teatree/explicit/slack-bot")


def _resp(body: dict) -> object:
    class _R:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return body

    return _R()
