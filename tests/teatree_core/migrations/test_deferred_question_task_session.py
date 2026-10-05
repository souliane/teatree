"""The ``0121`` migration leaves ``session_id`` naming only an asking Claude session.

A teatree Session number moves into ``task_session`` (dropped when it names no Session), the
news-batch, triage-batch and drafted-reply rows lose the loop session that claimed their task,
and the reverse puts the numbers back.
"""

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

_BEFORE = ("core", "0120_merge_banned_term_registry")
_AFTER = ("core", "0121_deferred_question_task_session")


@pytest.mark.timeout(240)
class TestTheAskingSessionColumnNamesOnlyAnAsker(TransactionTestCase):
    def setUp(self) -> None:
        self.addCleanup(self._restore_head)

    @staticmethod
    def _restore_head() -> None:
        connection.close()
        call_command("migrate", "core", "--no-input", verbosity=0)

    @staticmethod
    def _migrate_to(target: tuple[str, str]) -> dict[int, tuple[str, int | None]]:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate([target])
        question = (
            MigrationExecutor(connection).loader.project_state([target]).apps.get_model("core", "DeferredQuestion")
        )
        return {row.pk: (row.session_id, getattr(row, "task_session_id", None)) for row in question.objects.all()}

    def test_forward_moves_session_numbers_clears_loop_sessions_and_keeps_askers(self) -> None:
        executor = MigrationExecutor(connection)
        executor.migrate([_BEFORE])
        apps = executor.loader.project_state(_BEFORE).apps
        ticket = apps.get_model("core", "Ticket").objects.create()
        session = apps.get_model("core", "Session").objects.create(ticket=ticket, agent_id="coding")
        task = apps.get_model("core", "Task").objects.create(ticket=ticket, session=session, phase="triaging")
        loop = "loop-driving-session"
        seeded: dict[str, dict[str, object]] = {
            "by_task": {"question": "halt?", "session_id": str(session.pk)},
            "by_same_task": {"question": "stall?", "session_id": str(session.pk)},
            "orphan_number": {"question": "wedge?", "session_id": "999999"},
            "news": {"question": "approve?", "session_id": loop, "dedupe_marker": "news-batch-7"},
            "digit_news": {"question": "approve?", "session_id": str(session.pk), "dedupe_marker": "news-batch-9"},
            "triage": {
                "question": "Triaged 2.",
                "session_id": loop,
                "dedupe_marker": "triage-batch-8",
                "parked_task": task,
            },
            "draft": {"question": "Approve this drafted reply?\n\nsure", "session_id": loop, "parked_task": task},
            "orphan_draft": {"question": "Approve this drafted reply (thread 1.2)?\n\nno", "session_id": loop},
            "asked": {"question": "which host?", "session_id": "a0e4ab27-26ec-41bc-bb72"},
        }
        question = apps.get_model("core", "DeferredQuestion")
        pks = {name: question.objects.create(**fields).pk for name, fields in seeded.items()}

        rows = self._migrate_to(_AFTER)

        assert {name: rows[pk] for name, pk in pks.items()} == {
            "by_task": ("", session.pk),
            "by_same_task": ("", session.pk),
            "orphan_number": ("", None),
            "news": ("", None),
            "digit_news": ("", None),
            "triage": ("", None),
            "draft": ("", None),
            "orphan_draft": ("", None),
            "asked": ("a0e4ab27-26ec-41bc-bb72", None),
        }

    def test_reverse_puts_the_session_numbers_back(self) -> None:
        executor = MigrationExecutor(connection)
        executor.migrate([_BEFORE])
        apps = executor.loader.project_state(_BEFORE).apps
        ticket = apps.get_model("core", "Ticket").objects.create()
        session = apps.get_model("core", "Session").objects.create(ticket=ticket, agent_id="coding")
        by_task = (
            apps.get_model("core", "DeferredQuestion").objects.create(question="halt?", session_id=str(session.pk)).pk
        )
        self._migrate_to(_AFTER)

        rows = self._migrate_to(_BEFORE)

        assert rows[by_task] == (str(session.pk), None)
