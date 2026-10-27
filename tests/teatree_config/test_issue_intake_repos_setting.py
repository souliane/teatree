"""The issue-intake repo scope resolves as per-overlay operator data."""

from django.test import TestCase

from teatree.config import OVERLAY_OVERRIDABLE_SETTINGS, UserSettings, get_effective_settings
from teatree.core.models import ConfigSetting


class IssueIntakeReposSettingTests(TestCase):
    def test_empty_default_and_db_home_registration(self) -> None:
        assert UserSettings().issue_intake_repos == []
        assert "issue_intake_repos" in OVERLAY_OVERRIDABLE_SETTINGS

    def test_overlay_scope_wins_over_global_and_other_overlays_keep_global(self) -> None:
        ConfigSetting.objects.set_value("issue_intake_repos", ["o/global"], scope="")
        ConfigSetting.objects.set_value("issue_intake_repos", ["o/app"], scope="acme")

        assert get_effective_settings("acme").issue_intake_repos == ["o/app"]
        assert get_effective_settings("other").issue_intake_repos == ["o/global"]
