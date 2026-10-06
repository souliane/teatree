"""Migration 0002 blanks every stored ``self_pump`` loop-lease driver, the value it drops."""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from tests.teatree_core._migration_graph import core_initial_migration

_DROP = ("core", "0002_drop_self_pump_driver")


@pytest.mark.timeout(480)
class TestDropSelfPumpDriver(TransactionTestCase):
    def test_a_stored_self_pump_driver_is_blanked_and_others_are_kept(self) -> None:
        squash = ("core", core_initial_migration())
        executor = MigrationExecutor(connection)
        executor.migrate([squash])
        lease = executor.loader.project_state([squash]).apps.get_model("core", "LoopLease")
        lease.objects.create(name="t3-master", driver="self_pump")
        lease.objects.create(name="loop:dispatch", driver="loop_runner")

        executor = MigrationExecutor(connection)
        executor.migrate([_DROP])

        migrated = executor.loader.project_state([_DROP]).apps.get_model("core", "LoopLease")
        drivers = dict(migrated.objects.filter(name__in=["t3-master", "loop:dispatch"]).values_list("name", "driver"))
        assert drivers == {"t3-master": "", "loop:dispatch": "loop_runner"}
