"""The ``0105`` forward types the IN-FLIGHT rows that were already resumable.

Every row lands ``fresh`` by default, which is right for the shapes the old phase-equality
rule resumed by accident and wrong for the two it resumed on purpose: a queued needs-input
continuation would drop the owner's answer, and a limit-parked row would re-pay its whole
accumulated context as fresh input.

Driven through the real migration executor from ``0104``, which is the only run that
proves the deployed shape.
"""

from datetime import timedelta

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

_BEFORE = ("core", "0104_move_overlay_registry_pass_keys_onto_settings")
_AFTER = ("core", "0105_task_session_continuation")
#: main's TaskAttempt chain runs parallel to this one until ``0114`` joins them, so a
#: rewind to ``_BEFORE`` alone leaves its NOT NULL columns applied with no model field.
_PARALLEL_LEAF = ("core", "0092_taskattempt_taskattempt_recent_ended")

_ANSWER_REASON = "The user answered your earlier question: postgres-1. Continue from where you left off."


@pytest.mark.timeout(240)
class TestResumableTasksAreTyped(TransactionTestCase):
    def setUp(self) -> None:
        self.addCleanup(self._restore_head)

    @staticmethod
    def _restore_head() -> None:
        connection.close()
        call_command("migrate", "core", "--no-input", verbosity=0)

    def _rewind(self) -> MigrationExecutor:
        executor = MigrationExecutor(connection)
        executor.migrate([_BEFORE, _PARALLEL_LEAF])
        return executor

    @staticmethod
    def _models(executor: MigrationExecutor, state: list[tuple[str, str]]) -> tuple[type, type, type, type]:
        apps = executor.loader.project_state(state).apps
        return (
            apps.get_model("core", "Ticket"),
            apps.get_model("core", "Session"),
            apps.get_model("core", "Task"),
            apps.get_model("core", "TaskAttempt"),
        )

    def _seed(self, executor: MigrationExecutor) -> dict[str, int]:
        ticket_model, session_model, task_model, attempt_model = self._models(executor, [_BEFORE, _PARALLEL_LEAF])
        ticket = ticket_model.objects.create()
        session = session_model.objects.create(ticket=ticket)

        limit_parked = task_model.objects.create(
            ticket=ticket, session=session, status="pending", not_before=timezone.now() + timedelta(hours=1)
        )
        asked = task_model.objects.create(ticket=ticket, session=session, status="completed")
        attempt_model.objects.create(task=asked, result={"needs_user_input": True, "user_input_reason": "Which DB?"})
        answered = task_model.objects.create(
            ticket=ticket, session=session, status="pending", parent_task=asked, execution_reason=_ANSWER_REASON
        )
        answered_running = task_model.objects.create(
            ticket=ticket, session=session, status="claimed", parent_task=asked, execution_reason=_ANSWER_REASON
        )
        sibling = task_model.objects.create(
            ticket=ticket, session=session, status="pending", parent_task=asked, execution_reason="Repo: app"
        )
        return {
            "limit_parked": limit_parked.pk,
            "answered": answered.pk,
            "answered_running": answered_running.pk,
            "sibling": sibling.pk,
            "asked": asked.pk,
        }

    def _forward(self) -> dict[str, str]:
        executor = self._rewind()
        pks = self._seed(executor)

        executor.loader.build_graph()
        executor.migrate([_AFTER])

        _, _, task_model, _ = self._models(executor, [_AFTER])
        return {name: task_model.objects.get(pk=pk).session_continuation for name, pk in pks.items()}

    def test_a_queued_needs_input_continuation_keeps_its_parents_conversation(self) -> None:
        assert self._forward()["answered"] == "parent"

    def test_a_claimed_needs_input_continuation_keeps_its_parents_conversation(self) -> None:
        assert self._forward()["answered_running"] == "parent"

    def test_a_limit_parked_row_keeps_its_own_conversation(self) -> None:
        assert self._forward()["limit_parked"] == "self"

    def test_an_ordinary_same_parent_sibling_starts_fresh(self) -> None:
        assert self._forward()["sibling"] == "fresh"

    def test_the_already_finished_parent_is_left_fresh(self) -> None:
        assert self._forward()["asked"] == "fresh"
