"""teatree.core.models.loop_preset — Mode + ModeOverride behaviour.

The tri-state entry map (``state_for``), the overlay-scope accessor, the
single-live-override contract, and the low-token auto-engage manager methods
(#3159 item 6).
"""

import datetime as dt

import django.test
from django.utils import timezone

from teatree.core.models import ConfigSetting, Mode, ModeOverride


class TestModeCarriesOnlyTheLoopTable(django.test.SimpleTestCase):
    """#4202: a mode is its ``entries`` table plus an overlay scope — nothing else."""

    def test_no_posture_boolean_survives_on_the_model(self) -> None:
        field_names = {field.name for field in Mode._meta.get_fields()}
        assert {"defers_questions", "pauses_self_pump", "presence_sensitive"} & field_names == set()


class TestLoopPresetTriState(django.test.SimpleTestCase):
    def test_state_for_reads_true_false_and_inherit(self) -> None:
        preset = Mode(entries={"review": False, "dispatch": True})
        assert preset.state_for("review") is False
        assert preset.state_for("dispatch") is True
        assert preset.state_for("absent") is None

    def test_non_bool_value_degrades_to_inherit(self) -> None:
        preset = Mode(entries={"review": "off"})
        assert preset.state_for("review") is None

    def test_overlay_scope_names_filters_non_strings(self) -> None:
        assert Mode(overlay_scope=["a", "b", "", 3]).overlay_scope_names == ["a", "b"]
        assert Mode(overlay_scope="not-a-list").overlay_scope_names == []


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestLoopPresetOverride(django.test.TestCase):
    def test_set_override_keeps_a_single_row(self) -> None:
        ModeOverride.objects.set_override("a")
        ModeOverride.objects.set_override("b")
        assert ModeOverride.objects.count() == 1
        assert ModeOverride.objects.current().preset_name == "b"

    def test_current_ignores_expired(self) -> None:
        ModeOverride.objects.create(preset_name="a", until=timezone.now() - dt.timedelta(minutes=1))
        assert ModeOverride.objects.current() is None

    def test_hold_has_no_expiry(self) -> None:
        ModeOverride.objects.set_override("a")
        assert ModeOverride.objects.current().until is None


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestLowPowerAutoEngage(django.test.TestCase):
    def setUp(self) -> None:
        Mode.objects.create(name="low-token", entries={"inbox": True})
        self.reset = timezone.now() + dt.timedelta(hours=2)

    def _enable(self) -> None:
        ConfigSetting.objects.set_value("low_power_auto_engage", value=True)

    def test_no_op_when_flag_off(self) -> None:
        assert ModeOverride.objects.auto_engage_low_power(resets_at=self.reset) is False
        assert ModeOverride.objects.current() is None

    def test_engages_when_flag_on(self) -> None:
        self._enable()
        assert ModeOverride.objects.auto_engage_low_power(resets_at=self.reset) is True
        override = ModeOverride.objects.current()
        assert override.preset_name == "low-token"
        assert override.until == self.reset

    def test_never_overwrites_a_live_user_override(self) -> None:
        self._enable()
        ModeOverride.objects.set_override("present", reason="user hold")
        assert ModeOverride.objects.auto_engage_low_power(resets_at=self.reset) is False
        assert ModeOverride.objects.current().preset_name == "present"

    def test_no_op_when_target_preset_absent(self) -> None:
        self._enable()
        ConfigSetting.objects.set_value("low_power_preset_name", "ghost")
        assert ModeOverride.objects.auto_engage_low_power(resets_at=self.reset) is False

    def test_repointable_target_preset(self) -> None:
        self._enable()
        Mode.objects.create(name="frugal", entries={})
        ConfigSetting.objects.set_value("low_power_preset_name", "frugal")
        ModeOverride.objects.auto_engage_low_power(resets_at=self.reset)
        assert ModeOverride.objects.current().preset_name == "frugal"

    def test_clear_removes_only_an_auto_engaged_override(self) -> None:
        self._enable()
        ModeOverride.objects.auto_engage_low_power(resets_at=self.reset)
        assert ModeOverride.objects.clear_auto_engaged_low_power() is True
        assert ModeOverride.objects.current() is None

    def test_clear_leaves_a_user_override_intact(self) -> None:
        ModeOverride.objects.set_override("present", reason="user hold")
        assert ModeOverride.objects.clear_auto_engaged_low_power() is False
        assert ModeOverride.objects.current().preset_name == "present"
