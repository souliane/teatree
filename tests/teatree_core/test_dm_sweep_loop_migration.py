"""The ``0029`` data migration lands the new loop on an ALREADY-migrated database.

The seed inlined in ``0001_initial`` reaches only a fresh install, so a box already past
it would silently never get a ``dm_sweep`` row — the loop would be dark with no error.
These drive the real migration executor from ``0028`` forward, which is the only run
that proves the deployed shape. Anti-vacuous: dropping the ``RunPython`` leaves no
``dm_sweep`` row and the first test goes RED.
"""

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

_BEFORE = ("core", "0028_session_todo")
_AFTER = ("core", "0029_dm_sweep_loop_and_directive_cadence")


@pytest.mark.timeout(240)
class TestDmSweepLoopLandsOnAnExistingDatabase(TransactionTestCase):
    def setUp(self) -> None:
        self.addCleanup(self._restore_head)

    @staticmethod
    def _restore_head() -> None:
        connection.close()
        call_command("migrate", "core", "--no-input", verbosity=0)

    def _rewind(self) -> MigrationExecutor:
        executor = MigrationExecutor(connection)
        executor.migrate([_BEFORE])
        return executor

    @staticmethod
    def _loop_model(executor: MigrationExecutor, state: tuple[str, str]):
        return executor.loader.project_state(state).apps.get_model("core", "Loop")

    def test_an_existing_database_gains_the_dm_sweep_row(self) -> None:
        executor = self._rewind()
        self._loop_model(executor, _BEFORE).objects.filter(name="dm_sweep").delete()

        executor = MigrationExecutor(connection)
        executor.migrate([_AFTER])

        row = self._loop_model(executor, _AFTER).objects.get(name="dm_sweep")
        assert row.delay_seconds == 3600
        assert row.script == "src/teatree/loops/dm_sweep/loop.py"
        assert row.enabled is False
        assert row.description

    def test_rerunning_the_forward_creates_no_duplicate(self) -> None:
        import importlib  # noqa: PLC0415 — a numeric module name needs a runtime import

        module = importlib.import_module("teatree.core.migrations.0029_dm_sweep_loop_and_directive_cadence")

        executor = self._rewind()
        executor = MigrationExecutor(connection)
        executor.migrate([_AFTER])
        loop = self._loop_model(executor, _AFTER)

        module.forward(executor.loader.project_state(_AFTER).apps, connection.schema_editor())

        assert loop.objects.filter(name="dm_sweep").count() == 1

    @staticmethod
    def _seed_directive_loop(loop, *, delay_seconds: int) -> None:
        """Put the row at a known cadence.

        A sibling ``TransactionTestCase`` may have flushed the migration-seeded
        rows, so the fixture is created, not assumed.
        """
        loop.objects.update_or_create(
            name="directive_loop",
            defaults={"delay_seconds": delay_seconds, "script": "src/teatree/loops/directive_loop/loop.py"},
        )

    def test_the_directive_cadence_retune_respects_an_operator_edit(self) -> None:
        executor = self._rewind()
        self._seed_directive_loop(self._loop_model(executor, _BEFORE), delay_seconds=43200)

        executor = MigrationExecutor(connection)
        executor.migrate([_AFTER])

        assert self._loop_model(executor, _AFTER).objects.get(name="directive_loop").delay_seconds == 43200

    def test_the_directive_cadence_moves_off_the_old_default(self) -> None:
        executor = self._rewind()
        self._seed_directive_loop(self._loop_model(executor, _BEFORE), delay_seconds=86400)

        executor = MigrationExecutor(connection)
        executor.migrate([_AFTER])

        assert self._loop_model(executor, _AFTER).objects.get(name="directive_loop").delay_seconds == 3600
