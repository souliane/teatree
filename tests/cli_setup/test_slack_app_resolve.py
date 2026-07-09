"""Tests for ``teatree.cli.slack_app_resolve`` — shared app-id resolution (#1686).

The overlay registry is DB-home: reads/writes go through ``ConfigSetting`` and the
single ``overlays`` row (``{name: {fields}}``), so the config-touching classes are
DB-backed ``TestCase`` subclasses that seed via ``ConfigSetting.objects.set_value``.
"""

from unittest.mock import patch

import httpx
from django.test import TestCase

from teatree.cli.slack_app_resolve import (
    derive_app_id_from_token,
    persist_overlay_field,
    read_overlay_field,
    resolve_overlay_app_id,
    write_overlay_fields,
)
from teatree.core.models import ConfigSetting


def _seed(overlays: dict[str, dict]) -> None:
    ConfigSetting.objects.set_value("overlays", overlays)


def _registry() -> dict:
    return ConfigSetting.objects.get_effective("overlays") or {}


class TestReadOverlayField(TestCase):
    def test_returns_value_when_present(self) -> None:
        _seed({"t3": {"slack_app_id": "A123"}})
        assert read_overlay_field("t3", "slack_app_id") == "A123"

    def test_returns_empty_when_missing_overlay(self) -> None:
        _seed({"other": {"slack_app_id": "A123"}})
        assert read_overlay_field("t3", "slack_app_id") == ""

    def test_returns_empty_when_registry_empty(self) -> None:
        assert read_overlay_field("t3", "slack_app_id") == ""


class TestWriteOverlayFields(TestCase):
    def test_creates_entry_when_absent(self) -> None:
        write_overlay_fields("t3", {"slack_app_id": "A1", "messaging_backend": "slack"})
        assert _registry()["t3"] == {"slack_app_id": "A1", "messaging_backend": "slack"}

    def test_merges_into_existing_entry(self) -> None:
        _seed({"t3": {"slack_token_ref": "teatree/t3/slack"}})
        write_overlay_fields("t3", {"slack_app_id": "A1"})
        assert _registry()["t3"] == {"slack_token_ref": "teatree/t3/slack", "slack_app_id": "A1"}

    def test_leaves_other_overlays_untouched(self) -> None:
        _seed({"other": {"slack_app_id": "A0"}})
        write_overlay_fields("t3", {"slack_app_id": "A1"})
        assert _registry()["other"] == {"slack_app_id": "A0"}


class TestPersistOverlayField(TestCase):
    def test_writes_and_preserves_other_keys(self) -> None:
        _seed({"t3": {"slack_token_ref": "teatree/t3/slack"}})
        persist_overlay_field("t3", "slack_app_id", "A999")
        assert _registry()["t3"] == {"slack_token_ref": "teatree/t3/slack", "slack_app_id": "A999"}

    def test_noop_on_empty_value(self) -> None:
        _seed({"t3": {}})
        persist_overlay_field("t3", "slack_app_id", "")
        assert "slack_app_id" not in _registry()["t3"]

    def test_noop_when_overlay_block_absent(self) -> None:
        _seed({"other": {}})
        persist_overlay_field("t3", "slack_app_id", "A999")
        assert "t3" not in _registry()

    def test_noop_when_registry_empty(self) -> None:
        persist_overlay_field("t3", "slack_app_id", "A999")
        assert _registry() == {}


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


class TestResolveOverlayAppId(TestCase):
    def test_registry_value_wins(self) -> None:
        _seed({"t3": {"slack_app_id": "A_CFG"}})
        with patch("teatree.cli.slack_app_resolve.derive_app_id_from_token") as derive:
            assert resolve_overlay_app_id("t3") == "A_CFG"
            derive.assert_not_called()

    def test_derives_and_persists_when_registry_empty(self) -> None:
        _seed({"t3": {"slack_token_ref": "teatree/t3/slack"}})
        with (
            patch("teatree.cli.slack_app_resolve.read_pass", return_value="xoxb-tok"),
            patch("teatree.cli.slack_app_resolve.derive_app_id_from_token", return_value="A_DERIVED"),
        ):
            assert resolve_overlay_app_id("t3") == "A_DERIVED"
        assert _registry()["t3"]["slack_app_id"] == "A_DERIVED"

    def test_returns_empty_when_no_token_ref(self) -> None:
        _seed({"t3": {}})
        assert resolve_overlay_app_id("t3") == ""

    def test_uses_explicit_token_ref(self) -> None:
        _seed({"t3": {}})
        with (
            patch("teatree.cli.slack_app_resolve.read_pass", return_value="xoxb-tok") as read,
            patch("teatree.cli.slack_app_resolve.derive_app_id_from_token", return_value="A_X"),
        ):
            assert resolve_overlay_app_id("t3", token_ref="teatree/explicit/slack") == "A_X"
            read.assert_called_once_with("teatree/explicit/slack-bot")


def _resp(body: dict) -> object:
    class _R:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return body

    return _R()
