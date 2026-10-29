"""Audit-backed values used by the settings command."""

from django.test import TestCase

from teatree.core.models import ConfigSetting
from teatree.core.settings.config_setting_history import clear_with_history, latest_change, previous_value_after_set


class TestConfigSettingHistory(TestCase):
    def test_clear_reports_removed_and_prior_override(self) -> None:
        ConfigSetting.objects.set_value("agent_max_turns", 1000, authorized_by="operator")
        ConfigSetting.objects.set_value("agent_max_turns", 2000, authorized_by="operator")
        report = clear_with_history("agent_max_turns", "")
        assert report is not None
        assert (report.removed, report.previous) == ("2000", "1000")

    def test_legacy_row_has_unknown_prior_override(self) -> None:
        ConfigSetting.objects.create(key="agent_max_turns", value=400)
        report = clear_with_history("agent_max_turns", "")
        assert report is not None
        assert (report.removed, report.previous) == ("400", "unknown")

    def test_clear_without_a_row_does_not_create_history(self) -> None:
        assert clear_with_history("agent_max_turns", "") is None
        assert latest_change("agent_max_turns", "") is None

    def test_unchanged_set_reports_current_value_without_new_audit(self) -> None:
        ConfigSetting.objects.set_value("agent_max_turns", 1000, authorized_by="operator")
        before = latest_change("agent_max_turns", "")
        ConfigSetting.objects.set_value("agent_max_turns", 1000, authorized_by="operator")
        assert previous_value_after_set("agent_max_turns", "", before.pk if before else None, 1000) == "1000"

    def test_unchanged_sensitive_value_is_masked(self) -> None:
        assert previous_value_after_set("banned_term_registry", "", None, {"leak": ["private"]}) == "'***'"
