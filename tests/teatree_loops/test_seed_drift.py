"""Comparing a live mode/schedule VALUE against the shipped one (#4096).

``classification_drift`` answered this for ``Loop`` rows and one field. A mode's mask and
a calendar's slots were never compared at all, so the ``Mon-Fri 19:00 -> maintenance`` slot
that stalled the factory every night was structurally invisible to every surface.

Pure comparisons over plain values — the ORM-backed half is exercised through
``t3 loops audit`` in ``test_seed_inertness.py``.
"""

import datetime as dt

from teatree.loops.seed_drift import mode_entry_drift, schedule_slot_drift


class TestModeEntryDrift:
    def test_an_identical_mask_has_no_drift(self) -> None:
        assert mode_entry_drift({"ship": False, "inbox": True}, {"ship": False, "inbox": True}) == ()

    def test_a_flipped_opinion_names_both_sides(self) -> None:
        assert mode_entry_drift({"ship": False}, {"ship": True}) == ("ship shipped=false live=true",)

    def test_a_dropped_entry_reads_as_inheriting(self) -> None:
        assert mode_entry_drift({"issue_implementer": False}, {}) == (
            "issue_implementer shipped=false live=absent (inherits Loop.enabled)",
        )

    def test_an_entry_the_shipped_mask_never_had_is_named_too(self) -> None:
        assert mode_entry_drift({}, {"dream": True}) == ("dream shipped=absent (inherits Loop.enabled) live=true",)

    def test_a_corrupt_live_value_reads_as_inheriting_not_as_an_opinion(self) -> None:
        assert mode_entry_drift({"ship": True}, {"ship": "yes"}) == (
            "ship shipped=true live=absent (inherits Loop.enabled)",
        )

    def test_every_diverging_loop_is_named_in_a_stable_order(self) -> None:
        assert mode_entry_drift({"ship": False, "tickets": False}, {"ship": True, "tickets": True}) == (
            "ship shipped=false live=true",
            "tickets shipped=false live=true",
        )


class TestScheduleSlotDrift:
    def test_an_identical_calendar_has_no_drift(self) -> None:
        slots = [((0, 1, 2, 3, 4), dt.time(9, 0), "engaged")]
        assert schedule_slot_drift(slots, slots) == ()

    def test_the_slot_that_stalled_the_factory_is_named_as_added(self) -> None:
        shipped = [((0, 1, 2, 3, 4), dt.time(16, 0), "unattended")]
        live = [*shipped, ((0, 1, 2, 3, 4), dt.time(19, 0), "maintenance")]

        assert schedule_slot_drift(shipped, live) == ("adds Mon,Tue,Wed,Thu,Fri 19:00 -> maintenance",)

    def test_a_removed_shipped_slot_is_named_as_dropped(self) -> None:
        shipped = [((5, 6), dt.time(0, 0), "unattended")]

        assert schedule_slot_drift(shipped, []) == ("drops Sat,Sun 00:00 -> unattended",)

    def test_a_retimed_slot_reads_as_one_add_and_one_drop(self) -> None:
        assert schedule_slot_drift([((0,), dt.time(9, 0), "engaged")], [((0,), dt.time(10, 0), "engaged")]) == (
            "adds Mon 10:00 -> engaged",
            "drops Mon 09:00 -> engaged",
        )
