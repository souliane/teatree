"""`t3 <overlay> ticket show` — per-phase ``attempt N/max`` surfacing (#2009).

Closes the "no visible per-ticket/per-phase iteration budget" gap: the operator
can see how many attempts each phase has burned against the configurable cap.
"""

from typing import cast

import pytest
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from teatree.core.management.commands._ticket_show import TicketShowResult, render_ticket_show
from teatree.core.models import Session, Task, TaskAttempt, Ticket
from teatree.core.models.usage_window_state import LIMIT_PARKED_PREFIX


def _failed_attempt(task: Task, *, error: str) -> TaskAttempt:
    return TaskAttempt.objects.create(
        task=task,
        execution_target=task.execution_target,
        ended_at=timezone.now(),
        exit_code=1,
        error=error,
    )


def _park_attempt(task: Task) -> TaskAttempt:
    return TaskAttempt.objects.create(
        task=task,
        execution_target=task.execution_target,
        ended_at=timezone.now(),
        exit_code=1,
        error=f"{LIMIT_PARKED_PREFIX}session window active",
    )


@override_settings(MAX_PHASE_ITERATIONS=5)
class TicketShowTest(TestCase):
    def _phase_task(self, ticket: Ticket, *, phase: str) -> Task:
        session = Session.objects.create(ticket=ticket, agent_id="agent-1")
        return Task.objects.create(ticket=ticket, session=session, phase=phase)

    def test_show_reports_attempt_n_of_max_per_phase(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/1")
        coding = self._phase_task(ticket, phase="coding")
        _failed_attempt(coding, error="c1")
        _failed_attempt(coding, error="c2")
        result = cast(
            "dict[str, object]",
            call_command("ticket", "show", str(ticket.pk)),
        )
        phases = cast("list[dict[str, object]]", result["phases"])
        coding_row = next(row for row in phases if row["phase"] == "coding")
        assert coding_row["attempts"] == 2
        assert coding_row["max"] == 5

    def test_park_attempts_do_not_inflate_the_displayed_count(self) -> None:
        # #3689: limit-park attempts are scheduling events, not work iterations. The
        # budget query excludes them, so the displayed count must too — otherwise a
        # multi-hour usage-window outage that parked 200+ times showed "attempt 281/5".
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/park")
        coding = self._phase_task(ticket, phase="coding")
        _failed_attempt(coding, error="c1")
        for _ in range(200):
            _park_attempt(coding)
        _failed_attempt(coding, error="c2")
        result = cast(
            "dict[str, object]",
            call_command("ticket", "show", str(ticket.pk)),
        )
        phases = cast("list[dict[str, object]]", result["phases"])
        coding_row = next(row for row in phases if row["phase"] == "coding")
        assert coding_row["attempts"] == 2

    def test_show_renders_attempt_n_max_string(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/2")
        coding = self._phase_task(ticket, phase="coding")
        _failed_attempt(coding, error="c1")
        _failed_attempt(coding, error="c2")
        _failed_attempt(coding, error="c3")
        result = cast(
            "dict[str, object]",
            call_command("ticket", "show", str(ticket.pk)),
        )
        rendered = render_ticket_show(cast("TicketShowResult", result))
        assert "attempt 3/5" in rendered
        assert "coding" in rendered

    def test_show_unknown_ticket_exits_nonzero(self) -> None:
        with pytest.raises(SystemExit):
            call_command("ticket", "show", "999999")

    def test_show_includes_ticket_state(self) -> None:
        ticket = Ticket.objects.create(
            issue_url="https://example.com/issues/3",
            state=Ticket.State.STARTED,
        )
        result = cast(
            "dict[str, object]",
            call_command("ticket", "show", str(ticket.pk)),
        )
        assert result["ticket_id"] == int(ticket.pk)
        assert result["state"] == Ticket.State.STARTED
