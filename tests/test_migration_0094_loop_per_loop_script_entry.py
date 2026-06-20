"""Migration test for the per-loop ``script`` repoint (0094, #2513).

The #2550 cutover pointed every script-backed default ``Loop`` row at the SHARED
``src/teatree/loops/run.py``. This migration makes the column PER-LOOP: each
default script row moves to its OWN module ``src/teatree/loops/<name>/loop.py``.
The test pins it anti-vacuously — it lands a row holding the OLD shared value
before the migration and proves the forward migration repointed it to its own
module, the reverse restored the shared value, and an operator-edited row is left
untouched on both legs. ``enabled`` is never touched (the cutover stays paused).

``update_or_create`` is used for the default-named rows: the migration history at
the ``_BEFORE`` state already seeds them (0078 + later), so the test sets the
seeded row to the exact pre-migration value rather than creating a duplicate.
"""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class LoopPerLoopScriptEntryMigrationTest(TransactionTestCase):
    _BEFORE = ("core", "0093_collapse_agent_review_request_disabled")
    _AFTER = ("core", "0094_loop_per_loop_script_entry")
    _OLD_SHARED = "src/teatree/loops/run.py"
    _OLD_ARCH_PROMPT = "Run a sub-agent to run the arch_review loop."

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

    def test_forward_repoints_a_shared_runner_row_to_its_own_module(self) -> None:
        apps = self._migrate(self._BEFORE)
        loop = apps.get_model("core", "Loop")
        # A default script loop still holding the OLD shared value, plus an
        # operator-edited row with a custom script the migration must NOT touch.
        loop.objects.update_or_create(
            name="inbox",
            defaults={"script": self._OLD_SHARED, "prompt": None, "delay_seconds": 60, "enabled": False},
        )
        loop.objects.update_or_create(
            name="custom_op",
            defaults={"script": "operator/custom.py", "delay_seconds": 60, "enabled": False},
        )

        apps = self._migrate(self._AFTER)
        after = apps.get_model("core", "Loop")
        assert after.objects.get(name="inbox").script == "src/teatree/loops/inbox/loop.py"
        # The operator-edited row is left exactly as-is.
        assert after.objects.get(name="custom_op").script == "operator/custom.py"
        # No row anywhere still carries the retired shared runner.
        assert not after.objects.filter(script=self._OLD_SHARED).exists()
        # The cutover stays paused — enabled is never touched.
        assert after.objects.get(name="inbox").enabled is False
        self._restore_head()

    def test_reverse_restores_the_shared_runner_value(self) -> None:
        apps = self._migrate(self._AFTER)
        loop = apps.get_model("core", "Loop")
        # Land a default row at its AFTER-state per-loop module value, plus an
        # operator-edited row the reverse must leave alone.
        loop.objects.update_or_create(
            name="inbox",
            defaults={"script": "src/teatree/loops/inbox/loop.py", "prompt": None, "delay_seconds": 60},
        )
        loop.objects.update_or_create(
            name="custom_op",
            defaults={"script": "operator/custom.py", "delay_seconds": 60, "enabled": False},
        )

        apps = self._migrate(self._BEFORE)
        after = apps.get_model("core", "Loop")
        assert after.objects.get(name="inbox").script == self._OLD_SHARED
        assert after.objects.get(name="custom_op").script == "operator/custom.py"
        self._restore_head()

    def test_forward_rewrites_the_arch_review_prompt_body(self) -> None:
        # Self-contained (no reliance on ambient seeded data, which a sibling
        # migration test may have flushed): land an ``arch_review`` prompt-backed
        # row holding the exact OLD trivial body at BEFORE-state, then prove the
        # forward migration rewrites it to the ac-reviewing-codebase instruction.
        apps = self._migrate(self._BEFORE)
        loop = apps.get_model("core", "Loop")
        prompt = apps.get_model("core", "Prompt")
        body, _ = prompt.objects.update_or_create(name="arch_review", defaults={"body": self._OLD_ARCH_PROMPT})
        loop.objects.update_or_create(
            name="arch_review",
            defaults={"prompt": body, "script": "", "delay_seconds": 10800, "enabled": False},
        )

        apps = self._migrate(self._AFTER)
        after_loop = apps.get_model("core", "Loop")
        refreshed = after_loop.objects.select_related("prompt").get(name="arch_review")
        assert "ac-reviewing-codebase" in refreshed.prompt.body
        self._restore_head()

    def test_inlined_constants_match_the_install_seed(self) -> None:
        # The migration inlines the canonical values (a migration must not import
        # the evolving seed module); this pins they do not drift from the
        # install-time seed, so the migrate path and the squashed-install path
        # converge on the same per-loop scripts + arch_review prompt body.
        import importlib  # noqa: PLC0415

        from teatree.loops.seed import ARCH_REVIEW_PROMPT_BODY, script_entry_point_for  # noqa: PLC0415

        mig = importlib.import_module("teatree.core.migrations.0094_loop_per_loop_script_entry")
        assert mig._NEW_ARCH_REVIEW_PROMPT_BODY == ARCH_REVIEW_PROMPT_BODY
        assert mig._own_module("inbox") == script_entry_point_for("inbox")
        assert mig._own_module("dispatch") == script_entry_point_for("dispatch")
