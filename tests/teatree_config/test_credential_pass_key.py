"""A credential's ``pass`` entry name resolves through the settings layer, per venue."""

import types
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.config.credential_pass_key import PassKeySource, credential_of_setting, pass_key_setting
from teatree.core.models import ConfigSetting
from teatree.core.overlay import OverlayConfig


def _config(*, overlay: str = "acme", declared: dict[str, str] | None = None) -> OverlayConfig:
    config = OverlayConfig()
    for credential, entry in (declared or {}).items():
        config._register_secret(credential, entry)
    config.apply_toml_overrides(overlay)
    return config


class TestResolutionOrder(TestCase):
    def test_the_declared_default_answers_when_no_row_exists(self) -> None:
        resolution = _config(declared={"notion_token": "store/declared"}).resolve_pass_key("notion_token")

        assert (resolution.value, resolution.source) == ("store/declared", PassKeySource.DECLARED_DEFAULT)

    def test_an_overlay_scoped_row_beats_the_declared_default(self) -> None:
        ConfigSetting.objects.set_value("notion_token_pass_key", "venue/notion", scope="acme")

        resolution = _config(declared={"notion_token": "store/declared"}).resolve_pass_key("notion_token")

        assert (resolution.value, resolution.source) == ("venue/notion", PassKeySource.OVERLAY_DB)

    def test_a_global_row_applies_to_an_overlay_with_no_row_of_its_own(self) -> None:
        ConfigSetting.objects.set_value("notion_token_pass_key", "box/notion")

        resolution = _config(declared={"notion_token": "store/declared"}).resolve_pass_key("notion_token")

        assert (resolution.value, resolution.source) == ("box/notion", PassKeySource.GLOBAL_DB)

    def test_the_overlay_row_beats_the_global_row(self) -> None:
        ConfigSetting.objects.set_value("notion_token_pass_key", "box/notion")
        ConfigSetting.objects.set_value("notion_token_pass_key", "venue/notion", scope="acme")

        assert _config().secret_pass_key("notion_token") == "venue/notion"

    def test_a_row_scoped_to_another_overlay_does_not_bleed_in(self) -> None:
        ConfigSetting.objects.set_value("notion_token_pass_key", "other/notion", scope="other")

        resolution = _config(declared={"notion_token": "store/declared"}).resolve_pass_key("notion_token")

        assert resolution.value == "store/declared"

    def test_nothing_declared_and_nothing_stored_is_unset(self) -> None:
        resolution = _config().resolve_pass_key("notion_token")

        assert (resolution.value, resolution.source) == ("", PassKeySource.UNSET)
        assert resolution.setting == "notion_token_pass_key"

    def test_a_config_bound_to_no_overlay_answers_only_its_declared_default(self) -> None:
        ConfigSetting.objects.set_value("notion_token_pass_key", "box/notion")
        config = OverlayConfig()
        config._register_secret("notion_token", "store/declared")

        with patch("teatree.config.credential_pass_key.load_global_rows") as global_rows:
            resolution = config.resolve_pass_key("notion_token")

        assert (resolution.value, resolution.source) == ("store/declared", PassKeySource.DECLARED_DEFAULT)
        global_rows.assert_not_called()

    def test_an_unreadable_store_never_falls_back_onto_the_declared_default(self) -> None:
        config = _config(declared={"notion_token": "store/declared"})

        with patch("teatree.config.credential_pass_key.load_overlay_rows", return_value=({}, True)):
            resolution = config.resolve_pass_key("notion_token")

        assert (resolution.value, resolution.source) == ("", PassKeySource.UNREADABLE)


class TestSecretReadsFollowTheRouting(TestCase):
    def test_a_db_write_repoints_the_entry_a_getter_reads(self) -> None:
        config = _config(declared={"notion_token": "store/declared"})
        ConfigSetting.objects.set_value("notion_token_pass_key", "venue/notion", scope="acme")
        read = {}

        with patch("teatree.utils.secrets.read_pass", side_effect=lambda key: read.update(key=key) or "v"):
            config.get_notion_token()

        assert read["key"] == "venue/notion"

    def test_an_unconfigured_credential_reads_no_entry_at_all(self) -> None:
        with patch("teatree.utils.secrets.read_pass") as read_pass:
            assert _config().get_sentry_token() == ""

        read_pass.assert_not_called()


class TestTheOverlaysRegistryIsNoLongerAPassKeySource(TestCase):
    def test_a_pass_key_nested_in_the_overlays_registry_is_not_applied(self) -> None:
        registry = types.SimpleNamespace(raw={"overlays": {"acme": {"github_token_pass_key": "nested/entry"}}})

        with patch("teatree.config.load_config", return_value=registry):
            config = OverlayConfig(overlay_name="acme")

        assert config.resolve_pass_key("github_token").source is PassKeySource.UNSET


class TestSettingNames:
    @pytest.mark.parametrize(
        ("setting", "credential"),
        [("notion_token_pass_key", "notion_token"), ("notion_token", None), ("_pass_key", None), ("mode", None)],
    )
    def test_a_setting_maps_back_to_its_credential(self, setting: str, credential: str | None) -> None:
        assert credential_of_setting(setting) == credential

    def test_the_setting_name_is_the_credential_plus_the_suffix(self) -> None:
        assert pass_key_setting("sentry_token") == "sentry_token_pass_key"
