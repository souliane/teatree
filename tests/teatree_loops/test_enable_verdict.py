"""teatree.loops.enable_verdict — the ONE seam membership and admission share (#4185, #4196).

The verdict itself (hold > forced > mode mask > ``Loop.enabled``) and the proof that the
two readers of it cannot disagree. Membership used to resolve the mask through the L3/L2
PRESET layer while the tick resolved it through the merged MODE — so a schedule slot
upgraded by a live keystroke, and a box with no schedule at all, both produced a
membership set the tick contradicted, and the reconciler deleted the timers driving the
loops the tick was about to fire. Integration-first against the real DB.
"""

import django.test
from django.utils import timezone

from teatree.core.models import Loop, LoopState, Mode, ModeOverride
from teatree.loops.chain_membership import timer_chain_loop_names
from teatree.loops.enable_verdict import EnablePlanes, effective_verdicts
from teatree.loops.loop_table import admitted_loop_names
from tests.teatree_loops.mode_scenarios import LOOP, PRESENT_MODE, ModeWithoutOverrideMixin


def _loop(name: str, *, enabled: bool = True) -> Loop:
    return Loop.objects.create(name=name, delay_seconds=60, script=f"src/teatree/loops/{name}/loop.py", enabled=enabled)


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestEffectiveVerdicts(django.test.TestCase):
    def test_base_layer_when_no_mode_holds_an_opinion(self) -> None:
        _loop("ev-inbox")
        verdicts = {v.name: v for v in effective_verdicts()}
        assert verdicts["ev-inbox"].layer == "base"
        assert verdicts["ev-inbox"].admitted is True

    def test_hold_layer_wins_over_the_mask(self) -> None:
        _loop("ev-review")
        LoopState.objects.pause("ev-review")
        Mode.objects.create(name="engaged", entries={"ev-review": True})
        ModeOverride.objects.set_override("engaged")
        verdicts = {v.name: v for v in effective_verdicts()}
        assert verdicts["ev-review"].layer == "hold"
        assert verdicts["ev-review"].admitted is False

    def test_override_masks_a_loop_off(self) -> None:
        _loop("ev-review2")
        Mode.objects.create(name="heads-down", entries={"ev-review2": False})
        ModeOverride.objects.set_override("heads-down")
        verdicts = {v.name: v for v in effective_verdicts()}
        assert verdicts["ev-review2"].layer == "override"
        assert verdicts["ev-review2"].admitted is False


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestScheduleReachedModeIsUpgradedByPresence(ModeWithoutOverrideMixin):
    """A schedule slot + a live keystroke — the config a source-``override`` test cannot reach.

    The 23:30 ``maintenance`` slot with the owner at the keyboard: the mode resolver
    upgrades the away-class slot mode to the present-class one and admits the loop, while
    the preset resolver stops at the slot's own mode and masks it off. Membership built on
    the preset layer therefore excluded a loop the very next tick would fire — and
    ``ensure_loop_timers`` pruned its timer for not being a member.
    """

    def setUp(self) -> None:
        super().setUp()
        self.activate_away_schedule_slot()
        self.now = timezone.now()

    def test_the_upgraded_mode_decides_the_verdict(self) -> None:
        self.record_fresh_keystroke(self.now)
        verdict = next(v for v in effective_verdicts(self.now) if v.name == LOOP)
        assert verdict.admitted is True
        assert verdict.layer == "live"

    def test_membership_equals_the_ticks_admitted_set(self) -> None:
        self.record_fresh_keystroke(self.now)
        assert timer_chain_loop_names(self.now) == {LOOP}
        assert timer_chain_loop_names(self.now) == set(admitted_loop_names(self.now))

    def test_without_the_keystroke_the_slot_mode_masks_it_off_on_both_sides(self) -> None:
        # Anti-vacuous: the equality above is not an artefact of the loop being admitted
        # unconditionally — with no keystroke BOTH readers refuse it.
        assert timer_chain_loop_names(self.now) == set()
        assert admitted_loop_names(self.now) == []


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestNoScheduleFallsThroughToTheL0DefaultMode(ModeWithoutOverrideMixin):
    """``active_loop_schedule`` unset: the L0 ``default_mode`` row is the only opinion.

    The preset resolver returns ``None`` here and collapses membership to
    ``Loop.enabled``, while the tick reads the configured default mode — the variant in
    which the whole #4185 fix degraded to a no-op rather than merely disagreeing.
    """

    def setUp(self) -> None:
        super().setUp()
        self.use_l0_default_mode()
        self.now = timezone.now()

    def test_the_default_mode_decides_the_verdict(self) -> None:
        verdict = next(v for v in effective_verdicts(self.now) if v.name == LOOP)
        assert verdict.admitted is True
        assert verdict.layer == "default"

    def test_membership_equals_the_ticks_admitted_set(self) -> None:
        assert timer_chain_loop_names(self.now) == {LOOP}
        assert timer_chain_loop_names(self.now) == set(admitted_loop_names(self.now))

    def test_a_default_mode_that_masks_it_off_refuses_on_both_sides(self) -> None:
        Mode.objects.filter(name=PRESENT_MODE).update(entries={LOOP: False})
        Loop.objects.filter(name=LOOP).update(enabled=True)
        assert timer_chain_loop_names(self.now) == set()
        assert admitted_loop_names(self.now) == []


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestRefusalNamesTheArmThatDecided(ModeWithoutOverrideMixin):
    """The boolean and its explanation come off ONE call, so they cannot name different arms."""

    def test_the_mask_refusal_names_the_active_mode(self) -> None:
        self.activate_away_schedule_slot()
        planes = EnablePlanes.resolve(timezone.now())
        assert "masked off by the active preset/schedule" in planes.refusal(LOOP, configured_enabled=True)

    def test_an_admitted_loop_has_no_refusal(self) -> None:
        self.use_l0_default_mode()
        planes = EnablePlanes.resolve(timezone.now())
        assert planes.refusal(LOOP, configured_enabled=False) == ""
