"""teatree.core.models.loop_preset — Mode + ModeOverride behaviour.

The total entry map (``state_for``), the egress posture, the single-live-override
contract, and the token-outage auto-engage manager methods (#3159 item 6).
"""

import datetime as dt

import django.test
from django.utils import timezone

from teatree.core.models import ConfigSetting, Mode, ModeOverride


class TestModeCarriesOnlyTheLoopTableAndItsEgress(django.test.SimpleTestCase):
    """A mode is its ``entries`` table plus its egress posture — nothing else."""

    def test_no_posture_boolean_survives_on_the_model(self) -> None:
        field_names = {field.name for field in Mode._meta.get_fields()}
        assert {"defers_questions", "pauses_self_pump", "presence_sensitive"} & field_names == set()

    def test_a_mode_acts_on_the_owners_behalf_unless_it_says_otherwise(self) -> None:
        assert Mode(entries={}).forbids_egress is False
        assert Mode(entries={}, egress="forbid").forbids_egress is True


class TestLoopPresetIsTotal(django.test.SimpleTestCase):
    def test_state_for_reads_the_stored_opinion(self) -> None:
        preset = Mode(entries={"review": False, "dispatch": True})
        assert preset.state_for("review") is False
        assert preset.state_for("dispatch") is True

    def test_a_loop_the_table_never_names_reads_off(self) -> None:
        assert Mode(entries={}).state_for("absent") is False

    def test_a_non_bool_value_reads_off_rather_than_truthy(self) -> None:
        assert Mode(entries={"review": "off"}).state_for("review") is False


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestLoopPresetOverride(django.test.TestCase):
    def test_set_override_keeps_a_single_row(self) -> None:
        ModeOverride.objects.set_override("a", reason="test override")
        ModeOverride.objects.set_override("b", reason="test override")
        assert ModeOverride.objects.count() == 1
        assert ModeOverride.objects.current().preset_name == "b"

    def test_a_lapsed_expectation_never_clears_the_override(self) -> None:
        # A5/A7: no override auto-clears. `expected_lift_at` is advisory — the watcher
        # reminds against it, and the row stands until someone lifts it deliberately.
        ModeOverride.objects.set_override(
            "a", reason="test override", expected_lift_at=timezone.now() - dt.timedelta(minutes=1)
        )
        override = ModeOverride.objects.current()
        assert override is not None
        assert override.preset_name == "a"

    def test_an_override_set_without_an_expectation_carries_none(self) -> None:
        ModeOverride.objects.set_override("a", reason="test override")
        assert ModeOverride.objects.current().expected_lift_at is None


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestTokenOutageAutoEngage(django.test.TestCase):
    def setUp(self) -> None:
        Mode.objects.create(name="token-outage", entries={"inbox": True})
        self.reset = timezone.now() + dt.timedelta(hours=2)

    def _enable(self) -> None:
        ConfigSetting.objects.set_value("token_outage_auto_engage", value=True)

    def test_no_op_when_flag_off(self) -> None:
        assert ModeOverride.objects.auto_engage_token_outage(resets_at=self.reset) is False
        assert ModeOverride.objects.current() is None

    def test_engages_when_flag_on(self) -> None:
        self._enable()
        assert ModeOverride.objects.auto_engage_token_outage(resets_at=self.reset) is True
        override = ModeOverride.objects.current()
        assert override.preset_name == "token-outage"
        assert override.expected_lift_at == self.reset

    def test_never_overwrites_a_live_user_override(self) -> None:
        self._enable()
        ModeOverride.objects.set_override("present", reason="user hold")
        assert ModeOverride.objects.auto_engage_token_outage(resets_at=self.reset) is False
        assert ModeOverride.objects.current().preset_name == "present"

    def test_no_op_when_target_preset_absent(self) -> None:
        self._enable()
        ConfigSetting.objects.set_value("token_outage_preset_name", "ghost")
        assert ModeOverride.objects.auto_engage_token_outage(resets_at=self.reset) is False

    def test_repointable_target_preset(self) -> None:
        self._enable()
        Mode.objects.create(name="frugal", entries={})
        ConfigSetting.objects.set_value("token_outage_preset_name", "frugal")
        ModeOverride.objects.auto_engage_token_outage(resets_at=self.reset)
        assert ModeOverride.objects.current().preset_name == "frugal"

    def test_clear_removes_only_an_auto_engaged_override(self) -> None:
        self._enable()
        ModeOverride.objects.auto_engage_token_outage(resets_at=self.reset)
        assert ModeOverride.objects.clear_auto_engaged_token_outage() is True
        assert ModeOverride.objects.current() is None

    def test_clear_leaves_a_user_override_intact(self) -> None:
        ModeOverride.objects.set_override("present", reason="user hold")
        assert ModeOverride.objects.clear_auto_engaged_token_outage() is False
        assert ModeOverride.objects.current().preset_name == "present"
