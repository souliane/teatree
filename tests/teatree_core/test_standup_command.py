"""Tests for the ``standup`` management command (issue #563).

The command is a thin read-only wrapper over ``generate_standup``. It
returns JSON-serializable structures (codebase convention for
``django-typer`` commands — see ``followup``/``ticket``); markdown/JSON
*rendering* is covered at the generator level in ``test_standup``.
``call_command`` is invoked without ``stdout=`` capture on purpose:
``django-typer`` re-writes any truthy ``handle`` return through Django's
``OutputWrapper``, which only accepts strings (a pre-existing framework
quirk shared by every structured-return command here).
"""

from datetime import timedelta
from typing import cast

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from teatree.core.models.session import Session
from teatree.core.models.task import Task
from teatree.core.models.task_attempt import TaskAttempt
from teatree.core.models.ticket import Ticket
from teatree.core.models.transition import TicketTransition


class StandupCommandTests(TestCase):
    OVERLAY = "t3-teatree"

    def _ticket_with_recent_transition(self) -> Ticket:
        ticket = Ticket.objects.create(
            overlay=self.OVERLAY,
            issue_url="https://example.com/issues/42",
            state=Ticket.State.CODED,
        )
        tr = TicketTransition.objects.create(
            ticket=ticket,
            from_state=Ticket.State.STARTED,
            to_state=Ticket.State.CODED,
        )
        TicketTransition.objects.filter(pk=tr.pk).update(
            created_at=timezone.now() - timedelta(hours=2),
        )
        return ticket

    def test_generate_returns_json_safe_report(self) -> None:
        self._ticket_with_recent_transition()
        result = cast("dict[str, object]", call_command("standup", "generate"))
        assert isinstance(result, dict)
        yesterday = cast("list[dict[str, object]]", result["yesterday"])
        assert len(yesterday) == 1
        assert yesterday[0]["ticket_number"] == "42"
        assert result["blockers"] == []

    def test_since_option_overrides_window(self) -> None:
        self._ticket_with_recent_transition()
        old = (timezone.now() - timedelta(days=30)).isoformat()
        result = cast("dict[str, object]", call_command("standup", "generate", "--since", old))
        assert len(cast("list[object]", result["yesterday"])) == 1

    def test_naive_since_is_made_aware(self) -> None:
        self._ticket_with_recent_transition()
        # No timezone offset → naive datetime → command must localize it.
        naive = (timezone.now() - timedelta(days=2)).replace(tzinfo=None).isoformat()
        result = cast("dict[str, object]", call_command("standup", "generate", "--since", naive))
        assert len(cast("list[object]", result["yesterday"])) == 1
        assert "markdown" in result

    def test_days_option_widens_window(self) -> None:
        ticket = Ticket.objects.create(
            overlay=self.OVERLAY,
            issue_url="https://example.com/issues/7",
            state=Ticket.State.CODED,
        )
        tr = TicketTransition.objects.create(
            ticket=ticket,
            from_state=Ticket.State.STARTED,
            to_state=Ticket.State.CODED,
        )
        TicketTransition.objects.filter(pk=tr.pk).update(
            created_at=timezone.now() - timedelta(days=5),
        )
        narrow = cast("dict[str, object]", call_command("standup", "generate", "--days", "1"))
        assert narrow["yesterday"] == []
        wide = cast("dict[str, object]", call_command("standup", "generate", "--days", "10"))
        assert len(cast("list[object]", wide["yesterday"])) == 1

    def test_stale_subcommand_lists_idle_tickets(self) -> None:
        ticket = Ticket.objects.create(
            overlay=self.OVERLAY,
            issue_url="https://example.com/issues/99",
            state=Ticket.State.STARTED,
        )
        session = Session.objects.create(ticket=ticket, agent_id="a")
        task = Task.objects.create(ticket=ticket, session=session)
        att = TaskAttempt.objects.create(
            task=task,
            execution_target=Task.ExecutionTarget.HEADLESS,
            ended_at=timezone.now() - timedelta(days=9),
        )
        TaskAttempt.objects.filter(pk=att.pk).update(
            started_at=timezone.now() - timedelta(days=9),
        )
        rows = cast("list[dict[str, object]]", call_command("standup", "stale"))
        assert len(rows) == 1
        assert rows[0]["ticket_id"] == ticket.pk
        assert rows[0]["age_days"] == 9
        assert "summary" in rows[0]

    def test_stale_subcommand_respects_days_threshold(self) -> None:
        ticket = Ticket.objects.create(
            overlay=self.OVERLAY,
            issue_url="https://example.com/issues/55",
            state=Ticket.State.STARTED,
        )
        session = Session.objects.create(ticket=ticket, agent_id="a")
        task = Task.objects.create(ticket=ticket, session=session)
        att = TaskAttempt.objects.create(
            task=task,
            execution_target=Task.ExecutionTarget.HEADLESS,
            ended_at=timezone.now() - timedelta(days=4),
        )
        TaskAttempt.objects.filter(pk=att.pk).update(
            started_at=timezone.now() - timedelta(days=4),
        )
        assert call_command("standup", "stale", "--days", "7") == []
        assert len(cast("list[object]", call_command("standup", "stale", "--days", "2"))) == 1

    def test_command_does_not_mutate_state(self) -> None:
        ticket = self._ticket_with_recent_transition()
        call_command("standup", "generate")
        call_command("standup", "stale")
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.CODED
        assert TicketTransition.objects.count() == 1
