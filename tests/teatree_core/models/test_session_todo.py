"""The durable per-session working TODO (souliane/teatree#3572, directive #22).

The gap it closes: a background/headless session has NO harness TODO tool, so
its in-flight threads live as chat text and evaporate across turns. These
assert the durable half — the list survives a fresh read of the DB, keyed on a
`Session` that carries no harness-specific field.
"""

from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from teatree.core.models import Session, SessionTodo, Ticket


class SessionTodoModelTest(TestCase):
    def _session(self, agent_id: str = "entrypoint-abc") -> Session:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.STARTED)
        return Session.objects.create(ticket=ticket, agent_id=agent_id)

    def test_items_append_in_working_order(self) -> None:
        session = self._session()
        SessionTodo.objects.add(session, "first")
        SessionTodo.objects.add(session, "second")
        assert [t.text for t in SessionTodo.objects.open_for(session)] == ["first", "second"]

    def test_done_items_leave_the_open_list_but_survive(self) -> None:
        session = self._session()
        todo = SessionTodo.objects.add(session, "ship it")
        todo.set_status(SessionTodo.Status.DONE)
        assert list(SessionTodo.objects.open_for(session)) == []
        assert SessionTodo.objects.filter(session=session).count() == 1

    def test_the_list_reloads_from_the_db_not_from_memory(self) -> None:
        # The whole point: a session that lost its context still finds its threads.
        session = self._session()
        SessionTodo.objects.add(session, "resume the rebase")
        reloaded = Session.objects.get(pk=session.pk)
        assert [t.text for t in reloaded.todos.all()] == ["resume the rebase"]

    def test_a_second_session_does_not_see_the_first_ones_list(self) -> None:
        first, second = self._session("one"), self._session("two")
        SessionTodo.objects.add(first, "mine")
        assert list(SessionTodo.objects.open_for(second)) == []

    def test_the_anchor_carries_no_harness_specific_field(self) -> None:
        # Harness-agnostic by construction: `Session` identifies with a plain
        # `agent_id` string, so any harness reads and writes the same rows.
        field_names = {f.name for f in Session._meta.get_fields()}
        assert "agent_id" in field_names
        assert not any("claude" in name.lower() for name in field_names)


class SessionTodoCommandTest(TestCase):
    def _session(self) -> Session:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.STARTED)
        return Session.objects.create(ticket=ticket, agent_id="entrypoint-abc")

    def test_add_list_and_set_round_trip(self) -> None:
        session = self._session()
        call_command("session", "todo-add", "drain the queue", session_pk=session.pk)
        todo = SessionTodo.objects.get(session=session)
        call_command("session", "todo-set", str(todo.pk), "in_progress")
        todo.refresh_from_db()
        assert todo.status == SessionTodo.Status.IN_PROGRESS

    def test_unknown_status_is_refused(self) -> None:
        session = self._session()
        call_command("session", "todo-add", "x", session_pk=session.pk)
        todo = SessionTodo.objects.get(session=session)
        with pytest.raises(Exception, match="Unknown status"):
            call_command("session", "todo-set", str(todo.pk), "finished")

    def test_no_live_session_id_names_the_escape(self) -> None:
        # With no resolvable session the error must name `--session`, not leave the
        # caller guessing — a background session has no other way in.
        with (
            patch("teatree.core.management.commands.session.current_session_id", return_value=""),
            pytest.raises(CommandError, match="--session"),
        ):
            call_command("session", "todo-add", "x")
