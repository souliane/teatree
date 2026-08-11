"""A mode that stops the pipeline draining must not leave it filling (#4096).

The overnight ``maintenance`` window masked ``ship`` and ``tickets`` off but named no
opinion on ``issue_implementer``, so intake inherited ``Loop.enabled`` — which on that box
was ON — and kept claiming issues nothing could merge. Absent means INHERIT, so the base
flag is what decides whether an absent entry actually fills: the rule takes it as an input
rather than assuming the worst, or every legitimate mask would read as broken.
"""

from teatree.loops.mode_shape import (
    BACKUP_LOOP,
    DELIVERY_LOOPS,
    DISK_RECLAIM_LOOPS,
    FORCED_ON,
    INHERITS_OFF,
    INHERITS_ON,
    INTAKE_LOOPS,
    LOAD_BEARING_LOOPS,
    MASKED_OFF,
    backup_without_reclaim,
    intake_without_delivery,
    quieted_load_bearing,
)

_INTAKE_RUNNING = dict.fromkeys(INTAKE_LOOPS, True)
_INTAKE_PARKED = dict.fromkeys(INTAKE_LOOPS, False)


class TestIntakeWithoutDelivery:
    def test_the_maintenance_shape_is_named(self) -> None:
        found = intake_without_delivery(
            {"tickets": False, "ship": False, "review": False}, base_enabled=_INTAKE_RUNNING
        )

        assert found is not None
        assert found.masked_delivery == ("ship", "tickets")
        assert found.admitted_intake == (
            ("issue_implementer", "no entry, and its Loop row is enabled, so it inherits ON"),
        )

    def test_the_same_mask_is_clean_while_the_inherited_loop_is_off(self) -> None:
        """Absent means inherit — with the base flag off nothing fills, so nothing is wrong."""
        assert intake_without_delivery({"tickets": False, "ship": False}, base_enabled=_INTAKE_PARKED) is None

    def test_an_unknown_base_state_reads_as_not_running(self) -> None:
        assert intake_without_delivery({"ship": False}, base_enabled={}) is None

    def test_masking_the_intake_loop_off_too_is_clean(self) -> None:
        entries = {"ship": False, "tickets": False, "issue_implementer": False}

        assert intake_without_delivery(entries, base_enabled=_INTAKE_RUNNING) is None

    def test_a_mask_that_stops_nothing_draining_is_clean(self) -> None:
        assert intake_without_delivery({"ship": True, "tickets": True}, base_enabled=_INTAKE_RUNNING) is None

    def test_an_empty_mask_is_clean(self) -> None:
        assert intake_without_delivery({}, base_enabled=_INTAKE_RUNNING) is None

    def test_one_masked_delivery_loop_is_enough_to_ask_the_question(self) -> None:
        found = intake_without_delivery({"ship": False}, base_enabled=_INTAKE_RUNNING)

        assert found is not None
        assert found.masked_delivery == ("ship",)

    def test_intake_forced_on_under_masked_delivery_holds_whatever_the_base_flag_says(self) -> None:
        found = intake_without_delivery({"ship": False, "issue_implementer": True}, base_enabled=_INTAKE_PARKED)

        assert found is not None
        assert found.admitted_intake == (("issue_implementer", "forced on by the mask"),)

    def test_a_corrupt_value_reads_as_inherit_not_as_a_mask(self) -> None:
        """``Mode.state_for`` degrades a non-bool to inherit; this must agree with it."""
        entries = {"ship": "false", "issue_implementer": False}

        assert intake_without_delivery(entries, base_enabled=_INTAKE_RUNNING) is None

    def test_the_detail_names_both_halves_and_the_remedy(self) -> None:
        found = intake_without_delivery({"ship": False, "tickets": False}, base_enabled=_INTAKE_RUNNING)

        assert found is not None
        assert "ship" in found.detail
        assert "issue_implementer" in found.detail
        assert "nothing can merge" in found.detail
        assert "t3 loop preset edit" in found.detail

    def test_delivery_and_intake_do_not_overlap(self) -> None:
        assert not set(DELIVERY_LOOPS) & set(INTAKE_LOOPS)


_RECLAIM_RUNNING = dict.fromkeys((*DISK_RECLAIM_LOOPS, BACKUP_LOOP), True)


class TestBackupWithoutReclaim:
    """A mask that keeps WRITING backups with nothing left that can free the space (#4188)."""

    def test_the_off_shape_is_named(self) -> None:
        entries = {"db_backup": True, "resource_pressure": False, "idle_stack_reaper": False}

        found = backup_without_reclaim(entries, base_enabled=_RECLAIM_RUNNING)

        assert found is not None
        assert found.admitted_backup == FORCED_ON
        assert found.quieted_reclaim == (
            ("idle_stack_reaper", MASKED_OFF),
            ("resource_pressure", MASKED_OFF),
        )

    def test_an_inherited_backup_over_a_masked_reclaim_pair_counts(self) -> None:
        """Absent means inherit, and the shipped ``db_backup`` row is ON — so it still writes."""
        found = backup_without_reclaim(
            {"resource_pressure": False, "idle_stack_reaper": False}, base_enabled=_RECLAIM_RUNNING
        )

        assert found is not None
        assert found.admitted_backup == INHERITS_ON

    def test_one_surviving_reclaim_loop_is_enough_to_relieve_the_box(self) -> None:
        entries = {"db_backup": True, "resource_pressure": True, "idle_stack_reaper": False}

        assert backup_without_reclaim(entries, base_enabled=_RECLAIM_RUNNING) is None

    def test_a_masked_backup_over_a_quiet_reclaim_pair_is_clean(self) -> None:
        """``off`` may stop everything — what it may not do is keep writing."""
        entries = {"db_backup": False, "resource_pressure": False, "idle_stack_reaper": False}

        assert backup_without_reclaim(entries, base_enabled=_RECLAIM_RUNNING) is None

    def test_an_empty_mask_is_clean(self) -> None:
        assert backup_without_reclaim({}, base_enabled=_RECLAIM_RUNNING) is None

    def test_a_reclaim_loop_the_base_flags_do_not_name_reads_as_quiet(self) -> None:
        """The safety side fails LOUD: an unknown reclaim loop is not evidence of relief."""
        found = backup_without_reclaim({"db_backup": True}, base_enabled={})

        assert found is not None
        assert found.quieted_reclaim == (
            ("idle_stack_reaper", INHERITS_OFF),
            ("resource_pressure", INHERITS_OFF),
        )

    def test_an_unknown_backup_state_reads_as_not_writing(self) -> None:
        assert backup_without_reclaim({"resource_pressure": False, "idle_stack_reaper": False}, base_enabled={}) is None

    def test_a_corrupt_value_reads_as_inherit_not_as_a_mask(self) -> None:
        entries = {"db_backup": "true", "resource_pressure": False, "idle_stack_reaper": False}

        found = backup_without_reclaim(entries, base_enabled=_RECLAIM_RUNNING)

        assert found is not None
        assert found.admitted_backup == INHERITS_ON

    def test_the_detail_names_both_halves_and_the_remedy(self) -> None:
        entries = {"db_backup": True, "resource_pressure": False, "idle_stack_reaper": False}

        found = backup_without_reclaim(entries, base_enabled=_RECLAIM_RUNNING)

        assert found is not None
        assert "db_backup" in found.detail
        assert "resource_pressure" in found.detail
        assert "idle_stack_reaper" in found.detail
        assert "only ever consume disk" in found.detail
        assert "t3 loop preset edit" in found.detail

    def test_the_backup_loop_is_not_itself_a_reclaim_loop(self) -> None:
        assert BACKUP_LOOP not in DISK_RECLAIM_LOOPS


class TestQuietedLoadBearing:
    """The tier that keeps the BOX alive, which no mask may quiet (#4188)."""

    def test_a_masked_load_bearing_loop_is_named(self) -> None:
        assert quieted_load_bearing({"resource_pressure": False, "review": False}) == ("resource_pressure",)

    def test_every_masked_member_is_named_in_declaration_order(self) -> None:
        entries = dict.fromkeys(reversed(LOAD_BEARING_LOOPS), False)

        assert quieted_load_bearing(entries) == LOAD_BEARING_LOOPS

    def test_forcing_the_tier_on_is_clean(self) -> None:
        assert quieted_load_bearing(dict.fromkeys(LOAD_BEARING_LOOPS, True)) == ()

    def test_an_absent_entry_is_inherit_not_a_quieting(self) -> None:
        """The mask expresses no opinion — the loop's own column decides, as it always has."""
        assert quieted_load_bearing({"review": False}) == ()

    def test_a_corrupt_value_reads_as_inherit(self) -> None:
        assert quieted_load_bearing({"resource_pressure": "false"}) == ()

    def test_the_reclaim_pair_is_part_of_the_protected_tier(self) -> None:
        assert set(DISK_RECLAIM_LOOPS) <= set(LOAD_BEARING_LOOPS)
