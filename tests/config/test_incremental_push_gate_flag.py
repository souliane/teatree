# test-path: cross-cutting
"""The ``incremental_push_gate`` DB-home SETTLING feature flag (#122).

Default TRUE ⇒ the push gate scopes the diff (FULL on any uncertainty). The default
flipped ON once the CI ``selection-audit`` soak proved the scoped selection never
missed a whole-tree finding, so the flag graduated DARK → SETTLING: it survives as a
per-overlay escape hatch, DB-home + overridable, and ``off_value`` stays ``False``
(the value that means "gated code stays OFF" — the pre-#122 whole-tree run).
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
    def test_defaults_on(self) -> None:
        # #122: a feature behind a permanently-off flag is indistinguishable from
        # absent — a fresh/restored install with no ConfigSetting row resolves to
        # this dataclass default, so the scoped gate is actually live by default.
        assert UserSettings().incremental_push_gate is True

    def test_is_db_home(self) -> None:
        assert SETTING_HOMES["incremental_push_gate"] is SettingHome.DB

    def test_is_overlay_overridable(self) -> None:
        assert "incremental_push_gate" in OVERLAY_OVERRIDABLE_SETTINGS

    def test_is_a_settling_feature_flag_on_by_default(self) -> None:
        assert is_feature_flag("incremental_push_gate")
        flag = FEATURE_FLAGS["incremental_push_gate"]
        assert flag.stage is FlagStage.SETTLING
        assert flag.off_value is False
