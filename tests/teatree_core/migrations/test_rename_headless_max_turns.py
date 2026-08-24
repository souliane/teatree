"""The ``0068`` data migration carries a configured turn ceiling onto the unqualified key (#4212).

One execution lane leaves ``headless_max_turns`` qualifying nothing, so the key
becomes ``agent_max_turns``. A rename that does not move the stored row reverts the
operator to the shipped 250 in silence (#3527) — the box this lands on has 400 set.
Anti-vacuous: dropping the ``RunPython`` leaves no ``agent_max_turns`` row and the
first test goes RED.
"""

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

_BEFORE = ("core", "0067_drop_execution_target")
_AFTER = ("core", "0068_rename_headless_max_turns")


@pytest.mark.timeout(240)
class TestRenameHeadlessMaxTurns(TransactionTestCase):
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

    def test_a_configured_ceiling_moves_onto_the_unqualified_key(self) -> None:
        self._seed_before((("", "headless_max_turns", "400"),))

        rows = self._migrate_and_read()

        assert rows == {("", "agent_max_turns"): "400"}

    def test_each_scope_moves_independently(self) -> None:
        self._seed_before(
            (
                ("", "headless_max_turns", "400"),
                ("t3-teatree", "headless_max_turns", "120"),
            )
        )

        rows = self._migrate_and_read()

        assert rows == {("", "agent_max_turns"): "400", ("t3-teatree", "agent_max_turns"): "120"}

    def test_an_existing_row_under_the_canonical_key_wins(self) -> None:
        """The newer opinion is authoritative — the stale row is dropped, never promoted over it."""
        self._seed_before(
            (
                ("", "headless_max_turns", "400"),
                ("", "agent_max_turns", "90"),
            )
        )

        rows = self._migrate_and_read()

        assert rows == {("", "agent_max_turns"): "90"}

    def test_an_unrelated_row_is_untouched(self) -> None:
        self._seed_before((("", "wip", "slow"),))

        rows = self._migrate_and_read()

        assert rows == {("", "wip"): "slow"}
