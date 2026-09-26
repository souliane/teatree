"""A mask that keeps the box WRITING must leave something that can free the space (B4).

The ONE structural rule left. It is pure over a total mask: every loop is named, so
nothing has to be resolved against a base column to judge it.
"""

from teatree.loops.mode_shape import BACKUP_LOOP, DISK_RECLAIM_LOOPS, backup_without_reclaim


def _mask(**overrides: bool) -> dict[str, bool]:
    base = {BACKUP_LOOP: False, **dict.fromkeys(DISK_RECLAIM_LOOPS, True)}
    return {**base, **overrides}


class TestBackupWithoutReclaim:
    def test_a_writer_with_both_reclaim_loops_quiet_is_named(self) -> None:
        found = backup_without_reclaim(_mask(db_backup=True, idle_stack_reaper=False, resource_pressure=False))

        assert found is not None
        assert found.quieted_reclaim == DISK_RECLAIM_LOOPS
        assert BACKUP_LOOP in found.detail
        assert "t3 loop preset edit <mode> --set idle_stack_reaper=on" in found.detail

    def test_one_surviving_reclaim_loop_is_enough(self) -> None:
        assert backup_without_reclaim(_mask(db_backup=True, idle_stack_reaper=False)) is None

    def test_a_quiet_writer_is_never_the_shape(self) -> None:
        assert backup_without_reclaim(_mask(idle_stack_reaper=False, resource_pressure=False)) is None

    def test_a_mask_naming_nothing_is_never_the_shape(self) -> None:
        assert backup_without_reclaim({}) is None

    def test_the_all_off_off_preset_passes(self) -> None:
        assert backup_without_reclaim(dict.fromkeys((BACKUP_LOOP, *DISK_RECLAIM_LOOPS), False)) is None
