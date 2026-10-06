"""Migration 0003 deletes every stored ``drain_slot_reservation`` row, the setting it retires."""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

_BEFORE = ("core", "0002_drop_self_pump_driver")
_DELETE = ("core", "0003_delete_drain_slot_reservation_rows")


@pytest.mark.timeout(480)
class TestDeleteDrainSlotReservationRows(TransactionTestCase):
    def test_every_scope_of_the_retired_key_is_deleted_and_other_keys_are_kept(self) -> None:
        executor = MigrationExecutor(connection)
        executor.migrate([_BEFORE])
        setting = executor.loader.project_state([_BEFORE]).apps.get_model("core", "ConfigSetting")
        setting.objects.create(scope="", key="drain_slot_reservation", value=2)
        setting.objects.create(scope="t3-teatree", key="drain_slot_reservation", value=0)
        setting.objects.create(scope="", key="cheap_phase_admission_ceiling", value=3)

        executor = MigrationExecutor(connection)
        executor.migrate([_DELETE])

        migrated = executor.loader.project_state([_DELETE]).apps.get_model("core", "ConfigSetting")
        kept = migrated.objects.filter(key__in=["drain_slot_reservation", "cheap_phase_admission_ceiling"])
        assert list(kept.values_list("key", "value")) == [("cheap_phase_admission_ceiling", 3)]
