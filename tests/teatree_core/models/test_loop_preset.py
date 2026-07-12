"""teatree.core.models.loop_preset — LoopPreset + LoopPresetOverride behaviour.

The tri-state entry map (``state_for``), the availability pin / overlay-scope
accessors, the single-live-override contract, and the low-power auto-engage
manager methods (#3159 item 6).
"""

import datetime as dt

import django.test
from django.utils import timezone

from teatree.core import availability
from teatree.core.models import PIN_MODES, ConfigSetting, LoopPreset, LoopPresetOverride


class TestPinModesCanonical(django.test.SimpleTestCase):
    """LP-5: ONE canonical ``PIN_MODES`` drives pin validation (was triplicated + a dead copy)."""

    def test_pin_modes_are_the_availability_modes(self) -> None:
        # The pin set IS the availability-mode set; pinning to the canonical
        # ``VALID_MODES`` (not a hand-built copy) means a NEW availability mode
        # turns this red until the pin set adopts it — the two layer-separated
        # constants can never silently drift.
        assert PIN_MODES == availability.VALID_MODES

    def test_model_pin_validation_accepts_exactly_the_canonical_set(self) -> None:
        for mode in PIN_MODES:
            assert LoopPreset(availability_mode=mode).availability_pin == mode
        assert LoopPreset(availability_mode="not-a-mode").availability_pin is None

    def test_dead_pin_modes_copy_is_removed(self) -> None:
        from teatree.loops import preset_transitions  # noqa: PLC0415 — test-time module inspection

        assert not hasattr(preset_transitions, "_PIN_MODES")


class TestLoopPresetTriState(django.test.SimpleTestCase):
    def test_state_for_reads_true_false_and_inherit(self) -> None:
        preset = LoopPreset(entries={"review": False, "dispatch": True})
        assert preset.state_for("review") is False
        assert preset.state_for("dispatch") is True
        assert preset.state_for("absent") is None

    def test_non_bool_value_degrades_to_inherit(self) -> None:
        preset = LoopPreset(entries={"review": "off"})
        assert preset.state_for("review") is None

    def test_availability_pin_validates(self) -> None:
        assert LoopPreset(availability_mode="autonomous_away").availability_pin == "autonomous_away"
        assert LoopPreset(availability_mode="").availability_pin is None
        assert LoopPreset(availability_mode="bogus").availability_pin is None

    def test_overlay_scope_names_filters_non_strings(self) -> None:
        assert LoopPreset(overlay_scope=["a", "b", "", 3]).overlay_scope_names == ["a", "b"]
        assert LoopPreset(overlay_scope="not-a-list").overlay_scope_names == []


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestLoopPresetOverride(django.test.TestCase):
    def test_set_override_keeps_a_single_row(self) -> None:
        LoopPresetOverride.objects.set_override("a")
        LoopPresetOverride.objects.set_override("b")
        assert LoopPresetOverride.objects.count() == 1
        assert LoopPresetOverride.objects.current().preset_name == "b"

    def test_current_ignores_expired(self) -> None:
        LoopPresetOverride.objects.create(preset_name="a", until=timezone.now() - dt.timedelta(minutes=1))
        assert LoopPresetOverride.objects.current() is None

    def test_hold_has_no_expiry(self) -> None:
        LoopPresetOverride.objects.set_override("a")
        assert LoopPresetOverride.objects.current().until is None


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestLowPowerAutoEngage(django.test.TestCase):
    def setUp(self) -> None:
        LoopPreset.objects.create(name="low-power", entries={"inbox": True})
        self.reset = timezone.now() + dt.timedelta(hours=2)

    def _enable(self) -> None:
        ConfigSetting.objects.set_value("low_power_auto_engage", value=True)

    def test_no_op_when_flag_off(self) -> None:
        assert LoopPresetOverride.objects.auto_engage_low_power(resets_at=self.reset) is False
        assert LoopPresetOverride.objects.current() is None

    def test_engages_when_flag_on(self) -> None:
        self._enable()
        assert LoopPresetOverride.objects.auto_engage_low_power(resets_at=self.reset) is True
        override = LoopPresetOverride.objects.current()
        assert override.preset_name == "low-power"
        assert override.until == self.reset

    def test_never_overwrites_a_live_user_override(self) -> None:
        self._enable()
        LoopPresetOverride.objects.set_override("engaged", reason="user hold")
        assert LoopPresetOverride.objects.auto_engage_low_power(resets_at=self.reset) is False
        assert LoopPresetOverride.objects.current().preset_name == "engaged"

    def test_no_op_when_target_preset_absent(self) -> None:
        self._enable()
        ConfigSetting.objects.set_value("low_power_preset_name", "ghost")
        assert LoopPresetOverride.objects.auto_engage_low_power(resets_at=self.reset) is False

    def test_repointable_target_preset(self) -> None:
        self._enable()
        LoopPreset.objects.create(name="frugal", entries={})
        ConfigSetting.objects.set_value("low_power_preset_name", "frugal")
        LoopPresetOverride.objects.auto_engage_low_power(resets_at=self.reset)
        assert LoopPresetOverride.objects.current().preset_name == "frugal"

    def test_clear_removes_only_an_auto_engaged_override(self) -> None:
        self._enable()
        LoopPresetOverride.objects.auto_engage_low_power(resets_at=self.reset)
        assert LoopPresetOverride.objects.clear_auto_engaged_low_power() is True
        assert LoopPresetOverride.objects.current() is None

    def test_clear_leaves_a_user_override_intact(self) -> None:
        LoopPresetOverride.objects.set_override("engaged", reason="user hold")
        assert LoopPresetOverride.objects.clear_auto_engaged_low_power() is False
        assert LoopPresetOverride.objects.current().preset_name == "engaged"
