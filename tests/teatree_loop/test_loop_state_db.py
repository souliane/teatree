"""teatree.loop.loop_state_db — the single combined enable verdict over the DB.

``loop_state_admits`` is the ONE pure predicate every enable-decision site
resolves through: the single-lookup ``teatree.loops.enable_verdict.loop_admits`` (the off-live-tick
loop gates) and the live loop-table tick both apply it, so the verdict can never
drift into a tier-subset. ``loop_held_in_db`` is the durable per-loop
PAUSE/DISABLE read; it fails SAFE to no-hold on a read error but must WARN (not
whisper at debug) so a silently-unheld loop is observable (#2584 / holistic 3c#5).
"""

from unittest.mock import patch

import django.test

from teatree.core.models import LoopState
from teatree.loop.loop_state_db import loop_held_in_db, loop_state_admits


class TestLoopStateAdmits(django.test.SimpleTestCase):
    """The pure combined verdict: configured-enabled AND not runtime-held."""

    def test_configured_and_unheld_admits(self) -> None:
        assert loop_state_admits(configured_enabled=True, held=False, preset_state=None, forced=None) is True

    def test_held_is_not_admitted_even_when_configured(self) -> None:
        assert loop_state_admits(configured_enabled=True, held=True, preset_state=None, forced=None) is False

    def test_not_configured_is_not_admitted_even_when_unheld(self) -> None:
        assert loop_state_admits(configured_enabled=False, held=False, preset_state=None, forced=None) is False

    def test_not_configured_and_held_is_not_admitted(self) -> None:
        assert loop_state_admits(configured_enabled=False, held=True, preset_state=None, forced=None) is False

    def test_none_preset_is_byte_for_byte_the_two_plane_verdict(self) -> None:
        # The #3159 empty-table no-op: an explicit `preset_state=None` (what the
        # resolver returns with no preset) resolves exactly as the pre-#3159
        # `configured_enabled and not held`. There is no neutral default —
        # preset_state is required at every call site (the LP-3 structural guard).
        for configured in (True, False):
            for held in (True, False):
                assert loop_state_admits(configured_enabled=configured, held=held, preset_state=None, forced=None) == (
                    configured and not held
                )

    def test_preset_force_on_overrides_disabled_base(self) -> None:
        assert loop_state_admits(configured_enabled=False, held=False, preset_state=True, forced=None) is True

    def test_preset_force_off_overrides_enabled_base(self) -> None:
        assert loop_state_admits(configured_enabled=True, held=False, preset_state=False, forced=None) is False

    def test_hold_still_wins_over_a_force_on_preset(self) -> None:
        assert loop_state_admits(configured_enabled=True, held=True, preset_state=True, forced=None) is False


class TestLoopHeldFailsSafeButWarns(django.test.TestCase):
    """A per-loop PAUSE/DISABLE read error fails OPEN (no hold) — but WARNS, never whispers.

    The global kill-switch fails CLOSED on a read error; the symmetric per-loop
    hold fails OPEN so an unreadable DB can never silently disable a loop. That
    fail-open was swallowed at ``debug`` (#2584 / holistic 3c#5): a loop silently
    kept running with NO observable signal. It must log at WARNING so the operator
    can see the degraded read.
    """

    def test_read_error_returns_no_hold(self) -> None:
        with patch.object(LoopState.objects, "is_runnable", side_effect=RuntimeError("db down")):
            assert loop_held_in_db("review") is False

    def test_read_error_logs_at_warning(self) -> None:
        with (
            patch.object(LoopState.objects, "is_runnable", side_effect=RuntimeError("db down")),
            self.assertLogs("teatree.loop.loop_state_db", level="WARNING") as logs,
        ):
            loop_held_in_db("review")
        assert any("review" in line for line in logs.output)


class TestLoopHeldInDbResolvesDbTier(django.test.TestCase):
    """``loop_held_in_db`` is the ``LoopState`` arm of the tick gate (#1913).

    An empty table holds no loop (the default); a ``PAUSED`` / ``DISABLED`` row
    holds it — including the core ``dispatch`` loop (the restart-surviving 'pause
    everything', 2026-06-03 incident); ``resume`` / ``enable`` clears the hold.
    """

    def test_empty_table_holds_no_loop(self) -> None:
        assert loop_held_in_db("review") is False

    def test_empty_table_holds_not_the_dispatch_loop(self) -> None:
        assert loop_held_in_db("dispatch") is False

    def test_pause_holds_a_loop(self) -> None:
        LoopState.objects.pause("review")
        assert loop_held_in_db("review") is True

    def test_disable_holds_a_loop(self) -> None:
        LoopState.objects.disable("review")
        assert loop_held_in_db("review") is True

    def test_pause_holds_the_dispatch_loop(self) -> None:
        LoopState.objects.pause("dispatch")
        assert loop_held_in_db("dispatch") is True

    def test_disable_holds_the_dispatch_loop(self) -> None:
        LoopState.objects.disable("dispatch")
        assert loop_held_in_db("dispatch") is True

    def test_resume_clears_the_hold(self) -> None:
        LoopState.objects.pause("review")
        LoopState.objects.resume("review")
        assert loop_held_in_db("review") is False

    def test_resume_clears_the_hold_on_the_dispatch_loop(self) -> None:
        LoopState.objects.pause("dispatch")
        LoopState.objects.resume("dispatch")
        assert loop_held_in_db("dispatch") is False
