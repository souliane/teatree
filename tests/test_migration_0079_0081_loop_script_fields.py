"""Round-trip tests for the loop script-field migrations (0079-0081).

0079 adds the additive fields and makes ``delay_seconds`` nullable; 0080
backfills every seeded row to satisfy the prompt-XOR-script split (``arch_review``
keeps its prompt, the rest move to the script entry point); 0081 then adds the
DB-level check constraint. The migrations apply forward and reverse cleanly on a
scratch DB, and the backfill leaves every row XOR-valid.
"""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class LoopScriptFieldMigrationTest(TransactionTestCase):
    _PRE_SEED = ("core", "0077_loop")
    _SEED = ("core", "0078_seed_loops")
    _SCRIPT_FIELDS = ("core", "0079_loop_script_fields")
    _AFTER_BACKFILL = ("core", "0080_loop_backfill_prompt_script")
    _HEAD = ("core", "0081_loop_prompt_xor_script")

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

    def test_forward_to_head_applies_cleanly(self) -> None:
        self._migrate(self._PRE_SEED)
        apps = self._migrate(self._HEAD)
        loop = apps.get_model("core", "Loop")
        assert loop.objects.count() == 19
        self._restore_head()

    def test_backfill_gives_every_row_exactly_one_of_prompt_or_script(self) -> None:
        self._migrate(self._PRE_SEED)
        apps = self._migrate(self._AFTER_BACKFILL)
        loop = apps.get_model("core", "Loop")
        for row in loop.objects.all():
            assert bool(row.prompt) != bool(row.script), row.name
        self._restore_head()

    def test_backfill_keeps_arch_review_prompt_and_scripts_the_rest(self) -> None:
        self._migrate(self._PRE_SEED)
        apps = self._migrate(self._AFTER_BACKFILL)
        loop = apps.get_model("core", "Loop")
        arch = loop.objects.get(name="arch_review")
        assert arch.prompt != ""
        assert arch.script == ""
        dispatch = loop.objects.get(name="dispatch")
        assert dispatch.prompt == ""
        assert dispatch.script == "src/teatree/loops/run.py"
        self._restore_head()

    def test_backfill_leaves_custom_loop_present_before_0080_unchanged(self) -> None:
        self._migrate(self._PRE_SEED)
        apps = self._migrate(self._SCRIPT_FIELDS)
        loop = apps.get_model("core", "Loop")
        loop.objects.create(name="custom_user_loop", prompt="Run my custom loop.", delay_seconds=42)

        apps = self._migrate(self._AFTER_BACKFILL)
        custom_loop = apps.get_model("core", "Loop").objects.get(name="custom_user_loop")

        assert custom_loop.prompt == "Run my custom loop."
        assert custom_loop.script == ""
        assert custom_loop.delay_seconds == 42
        self._restore_head()

    def test_reverse_to_seed_state_restores_every_prompt(self) -> None:
        self._migrate(self._PRE_SEED)
        self._migrate(self._HEAD)
        apps = self._migrate(self._SEED)
        loop = apps.get_model("core", "Loop")
        assert loop.objects.count() == 19
        for row in loop.objects.all():
            assert row.prompt != ""
        self._restore_head()
