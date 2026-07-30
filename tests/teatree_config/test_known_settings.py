"""The unified known-key set unites all four config-key registries."""

from teatree.config import ALL_KNOWN_CONFIG_SETTINGS, COLD_HOOK_SETTINGS, OVERLAY_OVERRIDABLE_SETTINGS
from teatree.config.registries import COLD_SETTINGS, REGISTRY_SETTINGS
from teatree.core.management.commands.config_setting import _ALLOWED_SETTINGS


class TestAllKnownConfigSettings:
    def test_unites_all_four_registries(self) -> None:
        expected = (
            OVERLAY_OVERRIDABLE_SETTINGS.keys()
            | REGISTRY_SETTINGS.keys()
            | COLD_SETTINGS.keys()
            | COLD_HOOK_SETTINGS.keys()
        )

        assert ALL_KNOWN_CONFIG_SETTINGS.keys() == expected

    def test_cold_hook_keys_map_to_their_parsers(self) -> None:
        for key, setting in COLD_HOOK_SETTINGS.items():
            assert ALL_KNOWN_CONFIG_SETTINGS[key] is setting.parse

    def test_cli_command_consults_the_shared_set(self) -> None:
        assert _ALLOWED_SETTINGS is ALL_KNOWN_CONFIG_SETTINGS
