"""teatree.loops.preset_transitions — the side-effect-only transition chain (#3159, #61).

The chain never affects resolution (that is read-time); it reaps an expired
override, fires the deferred-question drain when a scheduled switch RETURNS the box
to reachable (the merged mode's ``defers_questions`` flips T->F), and posts one
Slack line per switch. The availability-pin push is GONE post-merge — the mode IS
availability. Notify + drain are patched at the boundary.
"""

import datetime as dt
from unittest.mock import patch

import django.test
from django.utils import timezone

from teatree.core.models import ConfigSetting, LoopPreset, LoopPresetOverride
from teatree.loops.preset_transitions import apply_preset_transition

_STAMP_KEY = "loop_preset_transition_stamp"
_DRAIN = "teatree.loops.preset_transitions.drain_deferred_questions"


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

    def test_switch_stamps_the_new_mode(self) -> None:
        self._activate("heads-down")
        with patch("teatree.loops.preset_transitions.notify_user"):
            outcome = apply_preset_transition(timezone.now())
        assert outcome["switched"] == "heads-down"
        assert ConfigSetting.objects.get_effective(_STAMP_KEY) == "heads-down"

    def test_second_pass_same_mode_is_unchanged(self) -> None:
        self._activate("heads-down")
        with patch("teatree.loops.preset_transitions.notify_user"):
            apply_preset_transition(timezone.now())
            outcome = apply_preset_transition(timezone.now())
        assert outcome["unchanged"] == 1

    def test_scheduled_return_to_reachable_drains(self) -> None:
        # Prior mode deferred (offline); the new active mode does not (engaged) —
        # the backlog drains, exactly as the old away->present transition did.
        LoopPreset.objects.update_or_create(
            name="offline", defaults={"entries": {}, "defers_questions": True, "pauses_self_pump": True}
        )
        LoopPreset.objects.update_or_create(name="engaged", defaults={"entries": {}, "defers_questions": False})
        ConfigSetting.objects.set_value(_STAMP_KEY, "offline")
        LoopPresetOverride.objects.set_override("engaged")
        with patch("teatree.loops.preset_transitions.notify_user"), patch(_DRAIN) as drain:
            apply_preset_transition(timezone.now())
        drain.assert_called_once()

    def test_no_drain_when_switching_between_deferring_modes(self) -> None:
        LoopPreset.objects.update_or_create(
            name="offline", defaults={"entries": {}, "defers_questions": True, "pauses_self_pump": True}
        )
        LoopPreset.objects.create(name="unattended", entries={}, defers_questions=True)
        ConfigSetting.objects.set_value(_STAMP_KEY, "offline")
        LoopPresetOverride.objects.set_override("unattended")
        with patch("teatree.loops.preset_transitions.notify_user"), patch(_DRAIN) as drain:
            apply_preset_transition(timezone.now())
        drain.assert_not_called()

    def test_no_drain_when_entering_a_deferring_mode(self) -> None:
        LoopPreset.objects.update_or_create(name="engaged", defaults={"entries": {}, "defers_questions": False})
        self._activate("unattended", defers_questions=True)
        ConfigSetting.objects.set_value(_STAMP_KEY, "engaged")
        with patch("teatree.loops.preset_transitions.notify_user"), patch(_DRAIN) as drain:
            apply_preset_transition(timezone.now())
        drain.assert_not_called()

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
        with patch("teatree.loops.preset_transitions.notify_user") as notify:
            apply_preset_transition(timezone.now())
        notify.assert_called_once()
