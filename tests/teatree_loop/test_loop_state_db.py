"""teatree.loop.loop_state_db — the single combined enable verdict over the DB.

``loop_state_admits`` is the ONE pure predicate every enable-decision site
resolves through: the single-lookup ``teatree.loops.enable_verdict.loop_admits`` (the off-live-tick
loop gates) and the live loop-table tick both apply it, so the verdict can never
drift into a tier-subset. ``loop_held_in_db`` is the durable per-loop
PAUSE/DISABLE read; under E3 it fails CLOSED on a read error — the hold stands and the
refusal logs at ERROR, because an unreadable brake is not evidence of no brake.
"""

from unittest.mock import patch

import django.test
import pytest

from teatree.core.models import Loop, LoopState
from teatree.loop.loop_state_db import (
    ControlPlanesUnreadableError,
    held_loop_names,
    loop_held_in_db,
    loop_state_admits,
    manual_override_map,
)


class TestLoopStateAdmits(django.test.SimpleTestCase):
    """The pure combined verdict: hold > manual override > preset."""

    def test_the_preset_decides_when_nobody_overrode(self) -> None:
        assert loop_state_admits(held=False, manual=None, preset_state=True) is True
        assert loop_state_admits(held=False, manual=None, preset_state=False) is False

    def test_a_manual_override_outranks_the_preset_in_both_directions(self) -> None:
        assert loop_state_admits(held=False, manual=True, preset_state=False) is True
        assert loop_state_admits(held=False, manual=False, preset_state=True) is False

    def test_a_hold_wins_over_everything_below_it(self) -> None:
        for manual in (True, False, None):
            for preset_state in (True, False):
                assert loop_state_admits(held=True, manual=manual, preset_state=preset_state) is False


class TestLoopHeldFailsClosedAndLoud(django.test.TestCase):
    """A per-loop PAUSE/DISABLE read error fails CLOSED (the hold stands) and logs at ERROR.

    These assertions are the INVERSE of the ones this class carried before E3, and the
    inversion is the evidence the doctrine changed rather than the code drifting: an
    unreadable row was read as "no hold", which is what a box with no hold also answers,
    so one database hiccup silently dropped the emergency brake and ran a held destructive
    loop. A brake this box cannot read is an incident, not a degraded read, so it is ERROR
    rather than WARNING.
    """

    def test_read_error_holds_the_loop(self) -> None:
        with patch.object(LoopState.objects, "is_runnable", side_effect=RuntimeError("db down")):
            assert loop_held_in_db("review") is True

    def test_read_error_logs_at_error(self) -> None:
        with (
            patch.object(LoopState.objects, "is_runnable", side_effect=RuntimeError("db down")),
            self.assertLogs("teatree.loop.loop_state_db", level="ERROR") as logs,
        ):
            loop_held_in_db("review")
        assert any("review" in line for line in logs.output)


class TestBulkControlReadsRaiseRatherThanAnswerEmpty(django.test.TestCase):
    """An unreadable control plane RAISES — an empty answer is what an unheld fleet gives.

    The three bulk reads used to return ``set()`` / ``{}`` / ``(set(), {})`` on a read
    error, which is byte-identical to a healthy box holding nothing. The caller therefore
    could not tell "nothing is held" from "the brake is unreadable" and ran everything on
    both readings.
    """

    def test_bulk_hold_read_raises(self) -> None:
        with (
            patch.object(LoopState.objects, "held_names", side_effect=RuntimeError("db down")),
            pytest.raises(ControlPlanesUnreadableError, match="hold"),
        ):
            held_loop_names()

    def test_bulk_manual_read_raises(self) -> None:
        with (
            patch.object(Loop.objects, "values_list", side_effect=RuntimeError("db down")),
            pytest.raises(ControlPlanesUnreadableError, match="manual override"),
        ):
            manual_override_map()

    def test_a_healthy_fleet_with_no_overrides_still_answers_empty(self) -> None:
        assert held_loop_names() == set()
        assert manual_override_map() == {}


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
