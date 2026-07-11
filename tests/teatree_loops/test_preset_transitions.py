"""teatree.loops.preset_transitions — the side-effect-only transition chain (#3159).

The chain never affects resolution (that is read-time); it reaps an expired
override, applies/clears the availability pin through ``write_override``, and posts
one Slack line per switch. Availability + notify are patched at the boundary so the
tests assert the DB-observable effects and the pin calls, not real I/O.
"""

import datetime as dt
from unittest.mock import patch

import django.test
from django.utils import timezone

from teatree.core.availability import Override
from teatree.core.models import ConfigSetting, LoopPreset, LoopPresetOverride
from teatree.loops.preset_transitions import apply_preset_transition

_STAMP_KEY = "loop_preset_transition_stamp"
_PIN_STAMP_KEY = "loop_preset_pin_stamp"


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestApplyPresetTransition(django.test.TestCase):
    def _activate(self, preset_name: str, **kwargs: object) -> None:
        LoopPreset.objects.get_or_create(name=preset_name, defaults={"entries": {}, **kwargs})
        LoopPresetOverride.objects.set_override(preset_name)

    def test_no_active_preset_is_unchanged(self) -> None:
        with patch("teatree.loops.preset_transitions.notify_user"):
            outcome = apply_preset_transition(timezone.now())
        assert outcome["unchanged"] == 1
        assert ConfigSetting.objects.get_effective(_STAMP_KEY) is None

    def test_switch_stamps_the_new_preset(self) -> None:
        self._activate("heads-down")
        with (
            patch("teatree.loops.preset_transitions.notify_user"),
            patch("teatree.core.availability.load_override", return_value=None),
        ):
            outcome = apply_preset_transition(timezone.now())
        assert outcome["switched"] == "heads-down"
        assert ConfigSetting.objects.get_effective(_STAMP_KEY) == "heads-down"

    def test_second_pass_same_preset_is_unchanged(self) -> None:
        self._activate("heads-down")
        with (
            patch("teatree.loops.preset_transitions.notify_user"),
            patch("teatree.core.availability.load_override", return_value=None),
        ):
            apply_preset_transition(timezone.now())
            outcome = apply_preset_transition(timezone.now())
        assert outcome["unchanged"] == 1

    def test_switch_applies_the_availability_pin(self) -> None:
        self._activate("unattended", availability_mode="autonomous_away")
        with (
            patch("teatree.loops.preset_transitions.notify_user"),
            patch("teatree.core.availability.load_override", return_value=None),
            patch("teatree.core.availability.write_override") as write,
        ):
            apply_preset_transition(timezone.now())
        write.assert_called_once()
        assert write.call_args.args[0] == "autonomous_away"
        assert ConfigSetting.objects.get_effective(_PIN_STAMP_KEY) == "autonomous_away"

    def test_user_written_override_is_never_overwritten(self) -> None:
        self._activate("unattended", availability_mode="autonomous_away")
        user_override = Override(mode="present", until=None)
        with (
            patch("teatree.loops.preset_transitions.notify_user"),
            patch("teatree.core.availability.load_override", return_value=user_override),
            patch("teatree.core.availability.write_override") as write,
        ):
            apply_preset_transition(timezone.now())
        write.assert_not_called()

    def test_expired_override_is_reaped(self) -> None:
        LoopPreset.objects.create(name="off", entries={})
        past = timezone.now() - dt.timedelta(hours=1)
        LoopPresetOverride.objects.create(preset_name="off", until=past)
        with patch("teatree.loops.preset_transitions.notify_user"):
            outcome = apply_preset_transition(timezone.now())
        assert outcome["reaped"] == 1
        assert LoopPresetOverride.objects.count() == 0

    def test_switch_posts_one_slack_line(self) -> None:
        self._activate("heads-down")
        with (
            patch("teatree.loops.preset_transitions.notify_user") as notify,
            patch("teatree.core.availability.load_override", return_value=None),
        ):
            apply_preset_transition(timezone.now())
        notify.assert_called_once()

    def test_clearing_a_preset_pin_when_the_preset_owns_it(self) -> None:
        # Preset previously pinned autonomous_away; now no preset is active and the
        # on-disk override still equals our pin, so the chain clears it.
        ConfigSetting.objects.set_value(_STAMP_KEY, "unattended")
        ConfigSetting.objects.set_value(_PIN_STAMP_KEY, "autonomous_away")
        preset_pin = Override(mode="autonomous_away", until=None)
        with (
            patch("teatree.loops.preset_transitions.notify_user"),
            patch("teatree.core.availability.load_override", return_value=preset_pin),
            patch("teatree.core.availability.clear_override") as clear,
        ):
            apply_preset_transition(timezone.now())
        clear.assert_called_once()
        assert ConfigSetting.objects.get_effective(_PIN_STAMP_KEY) is None
