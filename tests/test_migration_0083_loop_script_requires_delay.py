"""Migration tests for the script-loop delay invariant."""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class LoopScriptDelayMigrationTest(TransactionTestCase):
    _BEFORE = ("core", "0082_consolidatedmemory_disposition_and_more")
    _AFTER = ("core", "0083_loop_script_requires_delay")

    def _migrate(self, target: tuple[str, str]) -> "object":
        executor = MigrationExecutor(connection)
        executor.migrate([target])
        executor.loader.build_graph()
        return executor.loader.project_state([target]).apps

    def _restore_head(self) -> None:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        core_leaves = [node for node in executor.loader.graph.leaf_nodes() if node[0] == "core"]
        executor.migrate(core_leaves)

    def test_backfill_sets_delay_on_existing_script_loop_without_delay(self) -> None:
        apps = self._migrate(self._BEFORE)
        loop = apps.get_model("core", "Loop")
        loop.objects.create(name="custom_script_loop", prompt="", script="custom.py", delay_seconds=None)

        apps = self._migrate(self._AFTER)
        custom_loop = apps.get_model("core", "Loop").objects.get(name="custom_script_loop")

        assert custom_loop.delay_seconds == 60
        self._restore_head()

    def test_backfill_leaves_prompt_loop_without_delay_unchanged(self) -> None:
        apps = self._migrate(self._BEFORE)
        loop = apps.get_model("core", "Loop")
        loop.objects.create(name="custom_prompt_loop", prompt="Run custom prompt.", script="", delay_seconds=None)

        apps = self._migrate(self._AFTER)
        custom_loop = apps.get_model("core", "Loop").objects.get(name="custom_prompt_loop")

        assert custom_loop.prompt == "Run custom prompt."
        assert custom_loop.script == ""
        assert custom_loop.delay_seconds is None
        self._restore_head()
