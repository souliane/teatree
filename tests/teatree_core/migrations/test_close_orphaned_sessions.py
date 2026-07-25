"""The ``0032`` backfill closes the never-ended Sessions at their last activity.

``Session.ended_at`` had no production writer, so a deployed database holds one
open row per session ever minted (497 of 512 on the box this was found on) — each
pinning its ticket busy. The forward closes them; the tests pin the two properties
that make the backfill safe: it never stamps ``now()`` (that would re-pin every
row inside the staleness window) and it never closes a session that still owns
active work.

Driven through the real migration executor from ``0031``, which is the only run
that proves the deployed shape.
"""

import importlib
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

_BEFORE = ("core", "0031_taskattempt_reasoning_effort_and_more")
_AFTER = ("core", "0032_close_orphaned_sessions")


@pytest.mark.timeout(240)
class TestOrphanedSessionsAreClosedAtTheirLastActivity(TransactionTestCase):
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
    def _models(executor: MigrationExecutor, state: tuple[str, str]) -> tuple[type, type, type]:
        apps = executor.loader.project_state(state).apps
        return apps.get_model("core", "Ticket"), apps.get_model("core", "Session"), apps.get_model("core", "Task")

    @staticmethod
    def _open_session(session_model, ticket_model, *, age_hours: int):
        session = session_model.objects.create(ticket=ticket_model.objects.create())
        session_model.objects.filter(pk=session.pk).update(started_at=timezone.now() - timedelta(hours=age_hours))
        return session_model.objects.get(pk=session.pk)

    def _forward(self) -> MigrationExecutor:
        executor = MigrationExecutor(connection)
        executor.migrate([_AFTER])
        return executor

    def test_a_taskless_session_closes_at_its_own_start(self) -> None:
        executor = self._rewind()
        ticket_model, session_model, _task = self._models(executor, _BEFORE)
        session = self._open_session(session_model, ticket_model, age_hours=200)

        executor = self._forward()

        _t, session_model, _tk = self._models(executor, _AFTER)
        assert session_model.objects.get(pk=session.pk).ended_at == session.started_at

    def test_the_close_timestamp_is_never_now(self) -> None:
        executor = self._rewind()
        ticket_model, session_model, _task = self._models(executor, _BEFORE)
        session = self._open_session(session_model, ticket_model, age_hours=200)

        executor = self._forward()

        _t, session_model, _tk = self._models(executor, _AFTER)
        ended = session_model.objects.get(pk=session.pk).ended_at
        assert ended < timezone.now() - timedelta(hours=100), "closing at now() would re-pin the ticket"

    def test_a_session_owning_an_active_task_stays_open(self) -> None:
        executor = self._rewind()
        ticket_model, session_model, task_model = self._models(executor, _BEFORE)
        session = self._open_session(session_model, ticket_model, age_hours=200)
        task_model.objects.create(ticket=session.ticket, session=session, status="claimed")

        executor = self._forward()

        _t, session_model, _tk = self._models(executor, _AFTER)
        assert session_model.objects.get(pk=session.pk).ended_at is None

    def test_a_session_closes_at_its_latest_task_heartbeat(self) -> None:
        executor = self._rewind()
        ticket_model, session_model, task_model = self._models(executor, _BEFORE)
        session = self._open_session(session_model, ticket_model, age_hours=200)
        heartbeat = timezone.now() - timedelta(hours=50)
        task_model.objects.create(
            ticket=session.ticket,
            session=session,
            status="completed",
            heartbeat_at=heartbeat,
        )

        executor = self._forward()

        _t, session_model, _tk = self._models(executor, _AFTER)
        assert session_model.objects.get(pk=session.pk).ended_at == heartbeat

    def test_rerunning_the_forward_does_not_re_stamp_a_closed_session(self) -> None:
        executor = self._rewind()
        ticket_model, session_model, _task = self._models(executor, _BEFORE)
        session = self._open_session(session_model, ticket_model, age_hours=200)

        executor = self._forward()
        _t, session_model, _tk = self._models(executor, _AFTER)
        first = session_model.objects.get(pk=session.pk).ended_at

        module = importlib.import_module("teatree.core.migrations.0032_close_orphaned_sessions")
        module._close_orphaned_sessions(executor.loader.project_state(_AFTER).apps, connection.schema_editor())

        assert session_model.objects.get(pk=session.pk).ended_at == first
