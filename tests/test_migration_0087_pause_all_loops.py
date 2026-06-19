"""Migration test for the #2513 cutover-pause (0087).

The cutover is plumbing only: after migrate, NO loop is enabled. The test pins
this anti-vacuously — it creates an ENABLED loop before the migration and proves
the migration disabled it (a vacuous test on an already-paused DB would prove
nothing) — and that the reverse re-enables the default seeded set.
"""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class PauseAllLoopsMigrationTest(TransactionTestCase):
    _BEFORE = ("core", "0086_prompt_params_and_version")
    _AFTER = ("core", "0087_pause_all_loops")

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

    def test_forward_disables_every_enabled_loop(self) -> None:
        apps = self._migrate(self._BEFORE)
        loop = apps.get_model("core", "Loop")
        # A custom operator loop, explicitly ENABLED before the migration.
        loop.objects.create(name="custom_on", script="custom.py", delay_seconds=60, enabled=True)

        apps = self._migrate(self._AFTER)
        after = apps.get_model("core", "Loop")
        assert after.objects.get(name="custom_on").enabled is False
        # Nothing is left enabled anywhere — the cutover lands fully paused.
        assert not after.objects.filter(enabled=True).exists()
        self._restore_head()

    def test_reverse_reenables_default_seeded_loops(self) -> None:
        # Self-contained (no reliance on ambient migration-seeded data, which a
        # sibling migration test may have mutated): land a DEFAULT-named row
        # paused at AFTER-state, then prove the reverse re-enables it by name.
        apps = self._migrate(self._AFTER)
        loop = apps.get_model("core", "Loop")
        loop.objects.update_or_create(
            name="tickets",
            defaults={"script": "src/teatree/loops/run.py", "delay_seconds": 300, "enabled": False},
        )
        # A non-default custom row stays paused on reverse (reverse only touches
        # the default seeded set, never operator-created rows).
        loop.objects.update_or_create(
            name="custom_reverse",
            defaults={"script": "custom.py", "delay_seconds": 60, "enabled": False},
        )

        apps = self._migrate(self._BEFORE)
        after = apps.get_model("core", "Loop")
        assert after.objects.get(name="tickets").enabled is True
        assert after.objects.get(name="custom_reverse").enabled is False
        self._restore_head()
