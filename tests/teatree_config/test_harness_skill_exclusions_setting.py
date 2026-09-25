import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.config import OVERLAY_OVERRIDABLE_SETTINGS, UserSettings, get_effective_settings
from teatree.config.setting_groups import setting_group_path
from teatree.config.setting_help import setting_help
from teatree.config.setting_registries import BOX_GLOBAL_SETTINGS
from teatree.core.models import ConfigSetting


class TestHarnessSkillExclusionsSetting(TestCase):
    def test_default_registration_help_and_group_are_complete(self) -> None:
        assert UserSettings().harness_skill_exclusions == []
        assert "harness_skill_exclusions" in OVERLAY_OVERRIDABLE_SETTINGS
        assert "harness_skill_exclusions" in BOX_GLOBAL_SETTINGS
        assert "remove" in setting_help("harness_skill_exclusions")
        assert setting_group_path("harness_skill_exclusions") == ("Agents", "Mode & harness")

    def test_config_setting_set_persists_sorted_normalized_entries(self) -> None:
        call_command(
            "config_setting",
            "set",
            "harness_skill_exclusions",
            '[" CODEX:test ", "claude-code:review", "codex:test"]',
        )

        expected = ["claude-code:review", "codex:test"]
        assert ConfigSetting.objects.get(key="harness_skill_exclusions").value == expected
        assert get_effective_settings().harness_skill_exclusions == expected

    def test_overlay_scoped_exclusions_are_refused(self) -> None:
        with pytest.raises(ValueError, match="takes no overlay scope"):
            ConfigSetting.objects.set_value(
                "harness_skill_exclusions",
                ["codex:review"],
                scope="example",
            )

        assert not ConfigSetting.objects.filter(key="harness_skill_exclusions").exists()

    def test_config_setting_set_rejects_invalid_entries_without_writing(self) -> None:
        invalid_values = (
            '["cursor:review"]',
            '["codex:*"]',
            '["codex:"]',
            '["review"]',
            '"codex:review"',
        )

        for value in invalid_values:
            with self.subTest(value=value), pytest.raises(SystemExit):
                call_command("config_setting", "set", "harness_skill_exclusions", value)

        assert not ConfigSetting.objects.filter(key="harness_skill_exclusions").exists()
