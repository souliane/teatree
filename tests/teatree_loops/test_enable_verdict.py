"""teatree.loops.enable_verdict — the ONE seam membership and admission share (#4185, #4196).

The verdict itself (hold > manual override > preset) and the proof that the two readers
of it cannot disagree. Membership used to resolve the mask through the override/schedule
layer while the tick resolved it through the merged MODE — so a box with no schedule at
all produced a membership set the tick contradicted, and the reconciler deleted the timers
driving the loops the tick was about to fire. Integration-first against the real DB.
"""

from unittest.mock import patch

import django.test
from django.utils import timezone

from teatree.core.models import Loop, LoopState, Mode, ModeOverride, Prompt
from teatree.loops.chain_membership import timer_chain_loop_names
from teatree.loops.enable_verdict import EnablePlanes, effective_verdicts, loop_admits
from teatree.loops.loop_table import admitted_loop_names
from tests.teatree_loops.mode_scenarios import LOOP, PRESENT_MODE, ModeWithoutOverrideMixin


def _loop(name: str, *, enabled: bool | None = None) -> Loop:
    return Loop.objects.create(name=name, delay_seconds=60, script=f"src/teatree/loops/{name}/loop.py", enabled=enabled)


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestEffectiveVerdicts(django.test.TestCase):
    def test_the_preset_layer_decides_when_nobody_overrode(self) -> None:
        _loop("ev-inbox")
        Mode.objects.create(name="present", entries={"ev-inbox": True})
        ModeOverride.objects.set_override("present", reason="test override")
        verdicts = {v.name: v for v in effective_verdicts()}
        assert verdicts["ev-inbox"].layer == "override"
        assert verdicts["ev-inbox"].admitted is True

    def test_a_manual_override_names_itself_and_its_reason(self) -> None:
        _loop("ev-manual")
        Mode.objects.create(name="present", entries={"ev-manual": False})
        ModeOverride.objects.set_override("present", reason="test override")
        Loop.objects.set_manual_override("ev-manual", runs=True, reason="incident 42")
        verdicts = {v.name: v for v in effective_verdicts()}
        assert verdicts["ev-manual"].layer == "manual"
        assert verdicts["ev-manual"].detail == "manual override — incident 42"
        assert verdicts["ev-manual"].admitted is True

    def test_hold_layer_wins_over_the_mask(self) -> None:
        _loop("ev-review")
        LoopState.objects.pause("ev-review")
        Mode.objects.create(name="present", entries={"ev-review": True})
        ModeOverride.objects.set_override("present", reason="test override")
        verdicts = {v.name: v for v in effective_verdicts()}
        assert verdicts["ev-review"].layer == "hold"
        assert verdicts["ev-review"].admitted is False

    def test_override_masks_a_loop_off(self) -> None:
        _loop("ev-review2")
        Mode.objects.create(name="maintenance", entries={"ev-review2": False})
        ModeOverride.objects.set_override("maintenance", reason="test override")
        verdicts = {v.name: v for v in effective_verdicts()}
        assert verdicts["ev-review2"].layer == "override"
        assert verdicts["ev-review2"].admitted is False


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestNoScheduleFallsThroughToTheDefaultMode(ModeWithoutOverrideMixin):
    """``active_loop_schedule`` unset: the configured ``default_mode`` row is the only opinion.

    The preset resolver returns ``None`` here while the tick reads the configured default
    mode — the variant in which the whole #4185 fix degraded to a no-op rather than merely
    disagreeing.
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
        assert timer_chain_loop_names(self.now) == set()
        assert admitted_loop_names(self.now) == []


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestRefusalNamesTheArmThatDecided(ModeWithoutOverrideMixin):
    """The boolean and its explanation come off ONE call, so they cannot name different arms."""

    def test_the_mask_refusal_names_the_active_mode(self) -> None:
        self.activate_away_schedule_slot()
        planes = EnablePlanes.resolve(timezone.now())
        assert "masked off by the active preset" in planes.refusal(LOOP)

    def test_an_admitted_loop_has_no_refusal(self) -> None:
        self.use_l0_default_mode()
        planes = EnablePlanes.resolve(timezone.now())
        assert planes.refusal(LOOP) == ""


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestMembershipIsTheVerdictItself(ModeWithoutOverrideMixin):
    """One object, one answer — every arm moves on a durable write or a schedule boundary.

    Membership used to be a WIDER, presence-invariant closure because the live-presence
    upgrade flipped with no event to hook. With that arm gone every remaining arm has a
    chokepoint, so a chain built now is still answerable when it fires and the two
    readings are the same set.
    """

    def setUp(self) -> None:
        super().setUp()
        self.activate_away_schedule_slot()
        self.now = timezone.now()

    def test_a_masked_loop_is_neither_a_member_nor_admitted(self) -> None:
        assert admitted_loop_names(self.now) == []
        assert timer_chain_loop_names(self.now) == set()

    def test_a_manual_force_on_over_the_mask_is_both(self) -> None:
        Loop.objects.set_manual_override(LOOP, runs=True, reason="incident")
        assert admitted_loop_names(self.now) == [LOOP]
        assert timer_chain_loop_names(self.now) == {LOOP}

    def test_a_hold_still_loses_its_chain(self) -> None:
        Loop.objects.set_manual_override(LOOP, runs=True, reason="incident")
        LoopState.objects.pause(LOOP)
        assert timer_chain_loop_names(self.now) == set()

    def test_a_force_off_still_loses_its_chain(self) -> None:
        Loop.objects.set_manual_override(LOOP, runs=False, reason="test override")
        assert timer_chain_loop_names(self.now) == set()


@django.test.override_settings(USE_TZ=True)
class TestLoopEnabledCombinedVerdict(django.test.TestCase):
    """``loop_admits(name)`` is the one verdict: hold > manual override > preset."""

    def _loop(self, name: str, *, enabled: bool | None = None) -> Loop:
        prompt, _ = Prompt.objects.get_or_create(name=f"{name}-p", defaults={"body": "x"})
        return Loop.objects.update_or_create(
            name=name, defaults={"delay_seconds": 60, "prompt": prompt, "script": "", "enabled": enabled}
        )[0]

    def test_a_force_on_and_unheld_is_true(self) -> None:
        self._loop("le-on", enabled=True)
        assert loop_admits("le-on") is True

    def test_a_force_off_is_false(self) -> None:
        self._loop("le-off", enabled=False)
        assert loop_admits("le-off") is False

    def test_loopstate_hold_stops_a_force_on_loop(self) -> None:
        self._loop("le-held", enabled=True)
        LoopState.objects.disable("le-held")
        assert loop_admits("le-held") is False

    def test_missing_row_is_false(self) -> None:
        assert loop_admits("le-absent") is False

    def test_active_preset_force_off_masks_an_enabled_loop(self) -> None:
        self._loop("le-masked")
        Mode.objects.create(name="maintenance", entries={"le-masked": False})
        ModeOverride.objects.set_override("maintenance", reason="test override")
        assert loop_admits("le-masked") is False

    def test_active_preset_force_on_admits_an_un_overridden_loop(self) -> None:
        self._loop("le-forced")
        Mode.objects.create(name="present", entries={"le-forced": True})
        ModeOverride.objects.set_override("present", reason="test override")
        assert loop_admits("le-forced") is True

    def test_hold_beats_a_force_on_preset(self) -> None:
        self._loop("le-held-forced")
        LoopState.objects.disable("le-held-forced")
        Mode.objects.create(name="present", entries={"le-held-forced": True})
        ModeOverride.objects.set_override("present", reason="test override")
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

    ``loop_enabled`` resolved its own mask through the override/schedule layer, so the gate
    guarding ``outer_loop``'s own tick command could refuse what the fleet's verdict
    admitted, under exactly the configs that layer cannot see (#4196).
    """

    def test_it_agrees_under_the_default_mode(self) -> None:
        self.use_l0_default_mode()
        now = timezone.now()
        assert loop_admits(LOOP, now) is EnablePlanes.resolve(now).admits(LOOP)
        assert loop_admits(LOOP, now) is True

    def test_it_agrees_under_a_masking_schedule_slot(self) -> None:
        self.activate_away_schedule_slot()
        now = timezone.now()
        assert loop_admits(LOOP, now) is False
        assert LOOP not in timer_chain_loop_names(now)
