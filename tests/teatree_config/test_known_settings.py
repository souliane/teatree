"""The unified known-key set unites all four config-key registries."""

from teatree.config import ALL_KNOWN_CONFIG_SETTINGS, COLD_HOOK_SETTINGS, OVERLAY_OVERRIDABLE_SETTINGS
from teatree.config.known_settings import config_key_hint
from teatree.config.registries import COLD_SETTINGS, REGISTRY_SETTINGS
from teatree.core.management.commands.config_setting import _ALLOWED_SETTINGS


def test_provision_ram_percent_setting_is_retired() -> None:
    assert "provision_ram_ceiling_percent" not in ALL_KNOWN_CONFIG_SETTINGS
    assert config_key_hint("provision_ram_ceiling_percent").startswith("retired:")


class TestAllKnownConfigSettings:
    def test_unites_all_four_registries(self) -> None:
        expected = (
            OVERLAY_OVERRIDABLE_SETTINGS.keys()
            | REGISTRY_SETTINGS.keys()
            | COLD_SETTINGS.keys()
            | COLD_HOOK_SETTINGS.keys()
        )

        assert ALL_KNOWN_CONFIG_SETTINGS.keys() == expected

    def test_cold_keys_map_to_their_parsers_not_their_entries(self) -> None:
        # Both cold tables hold ``ColdHookSetting`` entries, and every consumer of this
        # union CALLS the value to validate a write — so unpacking a table whole would
        # bind the entry object and make every cold write raise.
        for key, setting in (*COLD_SETTINGS.items(), *COLD_HOOK_SETTINGS.items()):
            assert ALL_KNOWN_CONFIG_SETTINGS[key] is setting.parse

    def test_cli_command_consults_the_shared_set(self) -> None:
        assert _ALLOWED_SETTINGS is ALL_KNOWN_CONFIG_SETTINGS


class TestEveryUnknownKeySaysWhy:
    def test_a_renamed_key_names_its_successor(self) -> None:
        assert config_key_hint("speed") == "renamed to wip"

    def test_a_retired_key_names_why_and_its_issue(self) -> None:
        hint = config_key_hint("branch_prefix")
        assert hint.startswith("retired:")
        assert "#2731" in hint

    def test_any_other_key_is_named_undeclared(self) -> None:
        assert config_key_hint("not_a_setting_probe") == "not a declared setting"
