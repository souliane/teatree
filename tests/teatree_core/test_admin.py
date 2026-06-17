"""Django admin registrations for core models.

The autonomous-loop control plane (#1796) is manageable from the Django admin —
``Loop`` rows (name / prompt / delay / enabled) are added, edited, enabled, and
disabled there.
"""

from django.contrib import admin

from teatree.core.models import ConfigSetting, Loop


class TestConfigSettingAdmin:
    def test_config_setting_registered_in_admin(self) -> None:
        assert ConfigSetting in admin.site._registry

    def test_config_setting_admin_lists_and_edits_value(self) -> None:
        model_admin = admin.site._registry[ConfigSetting]
        assert "key" in model_admin.list_display
        assert "scope" in model_admin.list_display
        assert "value" in model_admin.list_editable


class TestLoopAdmin:
    def test_loop_registered_in_admin(self) -> None:
        assert Loop in admin.site._registry

    def test_loop_admin_lists_key_columns(self) -> None:
        model_admin = admin.site._registry[Loop]
        assert "name" in model_admin.list_display
        assert "enabled" in model_admin.list_display
        assert "delay_seconds" in model_admin.list_display

    def test_loop_admin_allows_inline_enable_disable(self) -> None:
        model_admin = admin.site._registry[Loop]
        assert "enabled" in model_admin.list_editable
