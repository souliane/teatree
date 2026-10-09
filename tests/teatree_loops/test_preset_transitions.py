"""teatree.loops.preset_transitions — the side-effect-only transition chain (#3159, #61).

The chain never affects resolution (that is read-time); it reaps an expired override
and posts one Slack line per switch. Notify is patched at the boundary.
"""

import datetime as dt
from unittest.mock import patch

import django.test
from django.utils import timezone

from teatree.core.mode_resolution import DEFAULT_MODE_SETTING
from teatree.core.models import ConfigSetting, Mode, ModeOverride
from teatree.loops.preset_transitions import apply_preset_transition

_STAMP_KEY = "loop_preset_transition_stamp"
_ENSURE_TIMERS = "teatree.loops.timer_reconciler.ensure_loop_timers"


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestApplyPresetTransition(django.test.TestCase):
    def _activate(self, preset_name: str, **kwargs: object) -> None:
        Mode.objects.get_or_create(name=preset_name, defaults={"entries": {}, **kwargs})
        ModeOverride.objects.set_override(preset_name, reason="test override")

    def test_no_active_preset_is_unchanged(self) -> None:
        with patch("teatree.loops.preset_transitions.notify_user"):
            outcome = apply_preset_transition(timezone.now())
        assert outcome["unchanged"] == 1
        assert ConfigSetting.objects.get_effective(_STAMP_KEY) is None

    def test_switch_stamps_the_new_mode(self) -> None:
        self._activate("away")
        with patch("teatree.loops.preset_transitions.notify_user"):
            outcome = apply_preset_transition(timezone.now())
        assert outcome["switched"] == "away"
        assert ConfigSetting.objects.get_effective(_STAMP_KEY) == "away"

    def test_second_pass_same_mode_is_unchanged(self) -> None:
        self._activate("away")
        with patch("teatree.loops.preset_transitions.notify_user"):
            apply_preset_transition(timezone.now())
            outcome = apply_preset_transition(timezone.now())
        assert outcome["unchanged"] == 1

    def test_a_standing_override_survives_the_pass(self) -> None:
        """A7: nothing reaps an override on a timer — only a person clears one."""
        Mode.objects.update_or_create(name="off", defaults={"entries": {}})
        ModeOverride.objects.set_override(
            "off", reason="holiday hold", expected_lift_at=timezone.now() - dt.timedelta(hours=1)
        )
        with patch("teatree.loops.preset_transitions.notify_user"):
            apply_preset_transition(timezone.now())
        assert ModeOverride.objects.count() == 1

    def test_switch_posts_one_slack_line(self) -> None:
        self._activate("away")
        with patch("teatree.loops.preset_transitions.notify_user") as notify:
            apply_preset_transition(timezone.now())
        notify.assert_called_once()


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestTransitionReconcilesTheChains(django.test.TestCase):
    """A switch re-heads the chains it just changed membership of (#4185).

    Chain membership is the preset verdict, so a switch that forces a loop ON leaves it
    driverless — and one that masks a loop OFF leaves a chain to prune — until something
    reconciles. The 5-minute reconcile chain would eventually, but this 60s chain is the
    switch's own chokepoint, so the new membership takes effect at the switch.
    """

    def test_a_switch_reconciles_the_timer_chains(self) -> None:
        Mode.objects.update_or_create(name="away", defaults={"entries": {}})
        ModeOverride.objects.set_override("away", reason="test override")
        with (
            patch("teatree.loops.preset_transitions.notify_user"),
            patch(_ENSURE_TIMERS) as ensure,
        ):
            apply_preset_transition(timezone.now())
        ensure.assert_called_once()

    def test_an_unchanged_pass_does_not_reconcile(self) -> None:
        # The FIRST pass always reconciles: an unstamped box has no recorded mode, and a
        # worker that just started must not assume the chains match whatever governs now.
        # Idempotence is the claim about the SECOND pass.
        with patch("teatree.loops.preset_transitions.notify_user"):
            apply_preset_transition(timezone.now())
            with patch(_ENSURE_TIMERS) as ensure:
                apply_preset_transition(timezone.now())
        ensure.assert_not_called()

    def test_an_l0_default_mode_change_reconciles(self) -> None:
        # The chokepoint was DEAD for this whole class of change: the preset layer returns
        # the same ``None`` before and after an L0 ``default_mode`` flip, so a stamp keyed
        # on it never fired while the mask — and therefore chain membership — moved (#4196).
        Mode.objects.update_or_create(name="l0-target", defaults={"entries": {}})
        with patch("teatree.loops.preset_transitions.notify_user"):
            apply_preset_transition(timezone.now())
            ConfigSetting.objects.set_value(DEFAULT_MODE_SETTING, "l0-target")
            with patch(_ENSURE_TIMERS) as ensure:
                outcome = apply_preset_transition(timezone.now())
        ensure.assert_called_once()
        assert outcome["reconciled"] == "l0-target"

    def test_an_l0_change_does_not_post_a_switch_line(self) -> None:
        # The two stamps are separate on purpose: the Slack line is about the OWNER's
        # own switch, so a mode change the owner never made must not post.
        Mode.objects.update_or_create(name="l0-quiet", defaults={"entries": {}})
        with patch("teatree.loops.preset_transitions.notify_user") as notify:
            apply_preset_transition(timezone.now())
            ConfigSetting.objects.set_value(DEFAULT_MODE_SETTING, "l0-quiet")
            notify.reset_mock()
            apply_preset_transition(timezone.now())
        notify.assert_not_called()

    def test_a_reconcile_failure_never_breaks_the_transition(self) -> None:
        Mode.objects.update_or_create(name="away", defaults={"entries": {}})
        ModeOverride.objects.set_override("away", reason="test override")
        with (
            patch("teatree.loops.preset_transitions.notify_user"),
            patch(_ENSURE_TIMERS, side_effect=RuntimeError("db down")),
        ):
            outcome = apply_preset_transition(timezone.now())
        assert outcome["switched"] == "away"
