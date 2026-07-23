"""The ``statusline_engaged_render`` DB-home opt-in flag (#3502).

Ships OFF, resolves from the ``ConfigSetting`` store, is registered in the
overridable registry so ``config_setting set`` can write it. It has NO env
override — the bash statusline reads it DB-only.
"""

import pytest
from django.test import TestCase

from teatree.config import ENV_SETTING_OVERRIDES, OVERLAY_OVERRIDABLE_SETTINGS, UserSettings, get_effective_settings
from teatree.config.homes import SETTING_HOMES, SettingHome
from teatree.core.models import ConfigSetting


class TestStatuslineEngagedRenderSetting(TestCase):
    def test_defaults_off(self) -> None:
        assert UserSettings().statusline_engaged_render is False

    def test_registered_overridable(self) -> None:
        assert "statusline_engaged_render" in OVERLAY_OVERRIDABLE_SETTINGS

    def test_no_env_override(self) -> None:
        assert all(field != "statusline_engaged_render" for field, _ in ENV_SETTING_OVERRIDES.values())

    def test_is_db_home(self) -> None:
        assert SETTING_HOMES["statusline_engaged_render"] is SettingHome.DB

    def test_resolves_from_the_db_store(self) -> None:
        ConfigSetting.objects.set_value("statusline_engaged_render", value=True)
        assert get_effective_settings().statusline_engaged_render is True

    def test_strict_bool_accepts_bool_rejects_quoted(self) -> None:
        parse = OVERLAY_OVERRIDABLE_SETTINGS["statusline_engaged_render"]
        enabled, disabled = True, False
        assert parse(enabled) is True
        assert parse(disabled) is False
        with pytest.raises(ValueError, match="Invalid bool value"):
            parse("true")
