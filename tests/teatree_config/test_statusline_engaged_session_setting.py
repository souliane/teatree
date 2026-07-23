"""The ``statusline_in_engaged_session`` DB-home statusline opt-in (#3502).

Ships OFF, resolves from the ``ConfigSetting`` store, and is registered in the
overridable registry so ``config_setting set`` can write it. Consumed only by the
bash ``statusline.sh`` render gate (its render behaviour is pinned in
``tests/test_teatree_opt_in.py::TestStatuslineGating``); this locks the config
round-trip the owner's ``config_setting set`` command relies on.
"""

from django.test import TestCase

from teatree.config import OVERLAY_OVERRIDABLE_SETTINGS, UserSettings, get_effective_settings
from teatree.core.models import ConfigSetting


class TestStatuslineInEngagedSessionSetting(TestCase):
    def test_defaults_off(self) -> None:
        assert UserSettings().statusline_in_engaged_session is False

    def test_registered_overridable(self) -> None:
        assert "statusline_in_engaged_session" in OVERLAY_OVERRIDABLE_SETTINGS

    def test_resolves_from_the_db_store(self) -> None:
        ConfigSetting.objects.set_value("statusline_in_engaged_session", value=True)
        assert get_effective_settings().statusline_in_engaged_session is True
