"""The ``0095`` data migration clears the stored rows under two removed keys.

A row under a removed key resolves to nothing and makes every resolution print a loud
stderr line, so a settled removal wants its rows gone. Anti-vacuous: dropping the
``RunPython`` leaves the rows in place and the first test goes RED, and the last test
proves the cleanup is keyed on the literal list rather than sweeping every row.
"""

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

_BEFORE = ("core", "0094_name_the_harness_control_timeout")
_AFTER = ("core", "0095_clear_retired_setting_rows")


@pytest.mark.timeout(240)
class TestClearRetiredSettingRows(TransactionTestCase):
    def setUp(self) -> None:
        self.addCleanup(self._restore_head)

    @staticmethod
    def _restore_head() -> None:
        connection.close()
        call_command("migrate", "core", "--no-input", verbosity=0)

    @staticmethod
    def _seed_before(rows: tuple[tuple[str, str, str], ...]) -> None:
        executor = MigrationExecutor(connection)
        executor.migrate([_BEFORE])
        config_setting = executor.loader.project_state(_BEFORE).apps.get_model("core", "ConfigSetting")
        config_setting.objects.all().delete()
        for scope, key, value in rows:
            config_setting.objects.create(scope=scope, key=key, value=value)

    @staticmethod
    def _migrate_and_read() -> dict[tuple[str, str], str]:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate([_AFTER])
        config_setting = (
            MigrationExecutor(connection).loader.project_state([_AFTER]).apps.get_model("core", "ConfigSetting")
        )
        return {(row.scope, row.key): row.value for row in config_setting.objects.all()}

    def test_a_stored_row_under_a_removed_key_is_cleared(self) -> None:
        self._seed_before((("", "limit_autorecovery_enabled", "true"),))

        assert self._migrate_and_read() == {}

    def test_every_scope_is_cleared_not_just_the_global_one(self) -> None:
        self._seed_before(
            (
                ("", "loop_runner_enabled", "true"),
                ("t3-teatree", "loop_runner_enabled", "false"),
            )
        )

        assert self._migrate_and_read() == {}

    def test_a_live_key_is_untouched(self) -> None:
        self._seed_before((("", "wip", "slow"),))

        assert self._migrate_and_read() == {("", "wip"): "slow"}
