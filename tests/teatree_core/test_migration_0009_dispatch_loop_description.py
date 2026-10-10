"""Migration 0009 rewrites the dispatch loop's description so it no longer says "deferred" (#4990).

Only a row still holding the shipped text is rewritten: a description an operator edited is left as written, and the
rewrite is reversible.
"""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

_BEFORE = ("core", "0008_deferred_question_evidence")
_REWRITE = ("core", "0009_dispatch_loop_description")
_PREFIX = "Runs the always-on global scanners every 5m: dispatches pending headless Tasks to phase sub-agents, "
_OLD = f"{_PREFIX}ingests incoming events, redelivers undelivered notifies, and posts deferred questions."
_NEW = f"{_PREFIX}ingests incoming events, redelivers undelivered notifies, and posts owner questions."


@pytest.mark.timeout(480)
class TestDispatchLoopDescription(TransactionTestCase):
    def setUp(self) -> None:
        self.addCleanup(self._migrate_to_graph_leaves)

    @staticmethod
    def _migrate_to_graph_leaves() -> None:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def _loop_model(self, state: tuple[str, str]) -> type:
        return MigrationExecutor(connection).loader.project_state([state]).apps.get_model("core", "Loop")

    def _migrate(self, target: tuple[str, str]) -> None:
        MigrationExecutor(connection).migrate([target])

    def _seed(self, description: str, *, name: str = "dispatch") -> None:
        self._migrate(_BEFORE)
        self._loop_model(_BEFORE).objects.filter(name=name).delete()
        self._loop_model(_BEFORE).objects.create(name=name, script="scan", delay_seconds=300, description=description)

    def test_the_shipped_text_is_rewritten(self) -> None:
        self._seed(_OLD)

        self._migrate(_REWRITE)

        assert self._loop_model(_REWRITE).objects.get(name="dispatch").description == _NEW
        assert "deferred" not in _NEW

    def test_an_operator_edited_description_is_left_as_written(self) -> None:
        self._seed("Runs everything, including deferred questions, as the operator describes it.")

        self._migrate(_REWRITE)

        assert "operator describes it" in self._loop_model(_REWRITE).objects.get(name="dispatch").description

    def test_another_loop_with_the_same_text_is_not_touched(self) -> None:
        self._seed(_OLD, name="tickets")

        self._migrate(_REWRITE)

        assert self._loop_model(_REWRITE).objects.get(name="tickets").description == _OLD

    def test_the_rewrite_is_reversible(self) -> None:
        self._seed(_OLD)
        self._migrate(_REWRITE)

        self._migrate(_BEFORE)

        assert self._loop_model(_BEFORE).objects.get(name="dispatch").description == _OLD
