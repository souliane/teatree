"""teatree.loops.enable_verdict — the ONE seam membership and admission share (#4185, #4196).

The verdict itself (hold > forced > mode mask > ``Loop.enabled``) and the proof that the
two readers of it cannot disagree. Membership used to resolve the mask through the L3/L2
PRESET layer while the tick resolved it through the merged MODE — so a schedule slot
upgraded by a live keystroke, and a box with no schedule at all, both produced a
membership set the tick contradicted, and the reconciler deleted the timers driving the
loops the tick was about to fire. Integration-first against the real DB.
"""

from unittest.mock import patch

import django.test
from django.utils import timezone

from teatree.core.models import Loop, LoopState, Mode, ModeOverride, Prompt
from teatree.loops.chain_membership import timer_chain_loop_names
from teatree.loops.enable_verdict import EnablePlanes, effective_verdicts, loop_admits
from teatree.loops.loop_table import admitted_loop_names
from tests.teatree_loops.mode_scenarios import AWAY_MODE, LOOP, PRESENT_MODE, ModeWithoutOverrideMixin


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
        Mode.objects.create(name="present", entries={"ev-review": True})
        ModeOverride.objects.set_override("present")
        verdicts = {v.name: v for v in effective_verdicts()}
        assert verdicts["ev-review"].layer == "hold"
        assert verdicts["ev-review"].admitted is False

    def test_override_masks_a_loop_off(self) -> None:
        _loop("ev-review2")
        Mode.objects.create(name="maintenance", entries={"ev-review2": False})
        ModeOverride.objects.set_override("maintenance")
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

    def test_without_the_keystroke_the_tick_refuses_while_the_chain_stays(self) -> None:
        # Anti-vacuous: the equality above is not an artefact of the loop being admitted
        # unconditionally — the slot's own mask DOES refuse the fire. The chain stays
        # anyway, which is the presence-invariant closure doing its job: pruning is the
        # destructive direction, so the keystroke must not be able to delete a timer.
        assert admitted_loop_names(self.now) == []
        assert timer_chain_loop_names(self.now) == {LOOP}


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


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestMembershipIsPresenceInvariant(ModeWithoutOverrideMixin):
    """Membership must not move when the keystroke does — the arm with no event to hook.

    A chain is built once and fires later. The presence upgrade is raised by a keystroke
    and lowered by the mere ABSENCE of one, so a point-in-time membership set flips
    between the build and the fire with nothing to reconcile on — and the reconciler's
    un-admitted arm DELETES the timer rather than leaving an idle one. Closing membership
    over both sides of the flip makes the driverless window zero by construction.
    """

    def setUp(self) -> None:
        super().setUp()
        self.activate_away_schedule_slot()
        self.now = timezone.now()

    def test_membership_is_identical_with_and_without_a_keystroke(self) -> None:
        away = timer_chain_loop_names(self.now)
        self.record_fresh_keystroke(self.now)
        assert timer_chain_loop_names(self.now) == away == {LOOP}

    def test_the_tick_still_refuses_the_fire_without_a_keystroke(self) -> None:
        # Anti-vacuous: membership is invariant precisely because it is WIDER — the
        # per-fire verdict does move, which is the whole point of keeping them apart.
        assert admitted_loop_names(self.now) == []
        self.record_fresh_keystroke(self.now)
        assert admitted_loop_names(self.now) == [LOOP]

    def test_membership_is_a_superset_of_admission_never_an_equality(self) -> None:
        assert set(admitted_loop_names(self.now)) < timer_chain_loop_names(self.now)

    def test_a_hold_still_loses_its_chain(self) -> None:
        # The closure covers the PRESENCE axis alone; every deliberate plane still prunes.
        LoopState.objects.pause(LOOP)
        assert timer_chain_loop_names(self.now) == set()

    def test_a_force_off_still_loses_its_chain(self) -> None:
        LoopState.objects.override(LOOP, on=False)
        assert timer_chain_loop_names(self.now) == set()

    def test_a_loop_both_sides_of_the_flip_mask_off_loses_its_chain(self) -> None:
        Mode.objects.filter(name=PRESENT_MODE).update(entries={LOOP: False})
        assert timer_chain_loop_names(self.now) == set()

    def test_an_override_has_no_alternate_so_nothing_is_widened(self) -> None:
        # A manual override is authoritative and never presence-upgraded, so the closure
        # collapses to the instant verdict — membership must not quietly widen under it.
        ModeOverride.objects.set_override(AWAY_MODE)
        assert EnablePlanes.resolve(self.now).resolved.presence_alternate is None
        assert timer_chain_loop_names(self.now) == set()


@django.test.override_settings(USE_TZ=True)
class TestLoopEnabledCombinedVerdict(django.test.TestCase):
    """``loop_admits(name)`` is ``Loop.enabled`` AND not ``LoopState``-held — one verdict."""

    def _loop(self, name: str, *, enabled: bool = True) -> Loop:
        prompt, _ = Prompt.objects.get_or_create(name=f"{name}-p", defaults={"body": "x"})
        return Loop.objects.update_or_create(
            name=name, defaults={"delay_seconds": 60, "prompt": prompt, "script": "", "enabled": enabled}
        )[0]

    def test_enabled_and_unheld_is_true(self) -> None:
        self._loop("le-on")
        assert loop_admits("le-on") is True

    def test_configured_disabled_is_false(self) -> None:
        self._loop("le-off", enabled=False)
        assert loop_admits("le-off") is False

    def test_loopstate_hold_stops_a_configured_loop(self) -> None:
        self._loop("le-held")
        LoopState.objects.disable("le-held")
        assert loop_admits("le-held") is False

    def test_missing_row_is_false(self) -> None:
        assert loop_admits("le-absent") is False

    def test_active_preset_force_off_masks_an_enabled_loop(self) -> None:
        self._loop("le-masked")
        Mode.objects.create(name="maintenance", entries={"le-masked": False})
        ModeOverride.objects.set_override("maintenance")
        assert loop_admits("le-masked") is False

    def test_active_preset_force_on_admits_a_disabled_loop(self) -> None:
        self._loop("le-forced", enabled=False)
        Mode.objects.create(name="present", entries={"le-forced": True})
        ModeOverride.objects.set_override("present")
        assert loop_admits("le-forced") is True

    def test_hold_beats_a_force_on_preset(self) -> None:
        self._loop("le-held-forced", enabled=False)
        LoopState.objects.disable("le-held-forced")
        Mode.objects.create(name="present", entries={"le-held-forced": True})
        ModeOverride.objects.set_override("present")
        assert loop_admits("le-held-forced") is False


class TestLoopAdmitsFailsSafeButWarns(django.test.TestCase):
    """LP-8: ``loop_admits``'s fail-open read error WARNS, symmetric with ``loop_held_in_db``.

    Both sibling reads fail OPEN (a hiccup never silently disables a loop), and the
    module's own doctrine (``loop_held_in_db``'s docstring) requires the swallow to
    be observable at WARNING — a loop silently mis-deciding is a real problem. The
    ``loop_enabled`` swallow logged at DEBUG before the move, whispering the same class of degraded
    read its sibling shouts.
    """

    def test_read_error_returns_enabled(self) -> None:
        with patch.object(Loop.objects, "filter", side_effect=RuntimeError("db down")):
            assert loop_admits("review") is True

    def test_read_error_logs_at_warning(self) -> None:
        with (
            patch.object(Loop.objects, "filter", side_effect=RuntimeError("db down")),
            self.assertLogs("teatree.loops.enable_verdict", level="WARNING") as logs,
        ):
            loop_admits("review")
        assert any("review" in line for line in logs.output)


@django.test.override_settings(USE_TZ=True)
class TestLoopAdmitsAgreesWithThePlanes(ModeWithoutOverrideMixin):
    """The single-lookup and the bulk read are one verdict — it used to be a third variant.

    ``loop_enabled`` resolved its own mask through the L3/L2 preset layer, so the gate
    guarding ``outer_loop``'s own tick command could refuse what the fleet's verdict
    admitted, under exactly the configs the preset layer cannot see (#4196).
    """

    def test_it_agrees_under_a_presence_upgraded_schedule_slot(self) -> None:
        self.activate_away_schedule_slot()
        self.record_fresh_keystroke()
        now = timezone.now()
        assert loop_admits(LOOP, now) is EnablePlanes.resolve(now).admits(LOOP, configured_enabled=False)
        assert loop_admits(LOOP, now) is True

    def test_it_agrees_under_the_l0_default_mode(self) -> None:
        self.use_l0_default_mode()
        now = timezone.now()
        assert loop_admits(LOOP, now) is EnablePlanes.resolve(now).admits(LOOP, configured_enabled=False)
        assert loop_admits(LOOP, now) is True

    def test_it_reads_the_narrow_verdict_not_membership(self) -> None:
        # Without a keystroke the away slot masks the loop off: the chain stays (the
        # closure) but the gate must refuse, or the off-live-tick commands would run
        # work the fleet's own tick declines.
        self.activate_away_schedule_slot()
        now = timezone.now()
        assert loop_admits(LOOP, now) is False
        assert LOOP in timer_chain_loop_names(now)
