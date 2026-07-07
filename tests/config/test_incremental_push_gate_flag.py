# test-path: cross-cutting
"""The ``incremental_push_gate`` DB-home DARK feature flag (#122).

Default FALSE ⇒ zero push-behaviour change on merge. Registered as a DARK feature
flag so it can never ship default-ON without a code-reviewed stage demotion, and
DB-home + per-overlay overridable so an operator flips it per-overlay once the CI
``selection-audit`` shows a clean soak window.
"""

from teatree.config import (
    OVERLAY_OVERRIDABLE_SETTINGS,
    SETTING_HOMES,
    FlagStage,
    SettingHome,
    UserSettings,
    is_feature_flag,
)
from teatree.config.feature_flags import FEATURE_FLAGS


class TestIncrementalPushGateFlag:
    def test_defaults_off(self) -> None:
        assert UserSettings().incremental_push_gate is False

    def test_is_db_home(self) -> None:
        assert SETTING_HOMES["incremental_push_gate"] is SettingHome.DB

    def test_is_overlay_overridable(self) -> None:
        assert "incremental_push_gate" in OVERLAY_OVERRIDABLE_SETTINGS

    def test_is_a_dark_feature_flag_off_by_default(self) -> None:
        assert is_feature_flag("incremental_push_gate")
        flag = FEATURE_FLAGS["incremental_push_gate"]
        assert flag.stage is FlagStage.DARK
        assert flag.off_value is False
