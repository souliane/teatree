"""Re-dispatch of stuck non-terminal tickets — the drain, hard-bounded (PR-5).

A non-terminal ticket with ZERO open tasks, no open PR, and no recent activity
is frozen: the FSM reads a work-state but nothing is scheduled to advance it, and
the report-only stale scanner never re-dispatches. This sweep schedules the task
the ticket's state implies (started→planning, planned→coding, …), bounded by the
#2009 repair budget, escalating LOUDLY via a ``DeferredQuestion`` when the budget
is exhausted — so a stuck ticket is drained or surfaced, never left silent.
"""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from teatree.core.models import Session, Task, TaskAttempt, Ticket
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.errors import InvalidTransitionError
from teatree.core.models.transition import TicketTransition
from teatree.core.repair_loop import max_phase_iterations
from teatree.loop.stuck_ticket_redispatch import (
    DEFAULT_STUCK_IDLE_HOURS,
    _idle_threshold_hours,
    redispatch_stuck_tickets,
)
from teatree.loop.tick_recovery import _reap_stale_task_claims


def _stuck_ticket(*, state: str = Ticket.State.STARTED, idle_hours: int = 48) -> Ticket:
    ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=state)
    transition = TicketTransition.objects.create(ticket=ticket, from_state="scoped", to_state=state)
    TicketTransition.objects.filter(pk=transition.pk).update(
        created_at=timezone.now() - timedelta(hours=idle_hours),
    )
    return ticket


class TestStuckTicketRedispatch(TestCase):
    def test_stuck_started_ticket_schedules_planning(self) -> None:
        ticket = _stuck_ticket(state=Ticket.State.STARTED)

        scheduled = redispatch_stuck_tickets()

        assert scheduled == 1
        planning = ticket.tasks.filter(phase="planning", status=Task.Status.PENDING)
        assert planning.count() == 1

    def test_stuck_planned_ticket_schedules_coding(self) -> None:
        ticket = _stuck_ticket(state=Ticket.State.PLANNED)

        assert redispatch_stuck_tickets() == 1
        assert ticket.tasks.filter(phase="coding", status=Task.Status.PENDING).count() == 1

    def test_stuck_coded_ticket_schedules_testing(self) -> None:
        ticket = _stuck_ticket(state=Ticket.State.CODED)

        assert redispatch_stuck_tickets() == 1
        assert ticket.tasks.filter(phase="testing", status=Task.Status.PENDING).count() == 1

    def test_stuck_tested_ticket_schedules_reviewing(self) -> None:
        ticket = _stuck_ticket(state=Ticket.State.TESTED)

        assert redispatch_stuck_tickets() == 1
        assert ticket.tasks.filter(phase="reviewing", status=Task.Status.PENDING).count() == 1

    def test_stuck_reviewed_ticket_schedules_shipping(self) -> None:
        ticket = _stuck_ticket(state=Ticket.State.REVIEWED)

        assert redispatch_stuck_tickets() == 1
        assert ticket.tasks.filter(phase="shipping", status=Task.Status.PENDING).count() == 1

    def test_ticket_with_no_activity_record_is_left_alone(self) -> None:
        # No transition and no task means idleness cannot be measured; the sweep
        # must not re-dispatch a ticket it cannot prove is stale.
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.STARTED)

        assert redispatch_stuck_tickets() == 0
        assert ticket.tasks.count() == 0

    def test_scheduling_refusal_is_escalated(self) -> None:
        _stuck_ticket(state=Ticket.State.STARTED)

        with patch.object(Ticket, "schedule_planning", side_effect=InvalidTransitionError("gate refused")):
            scheduled = redispatch_stuck_tickets()

        assert scheduled == 0
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 1

    def test_bad_idle_threshold_setting_falls_back_to_default(self) -> None:
        with override_settings(STUCK_TICKET_IDLE_HOURS="not-a-number"):
            assert _idle_threshold_hours() == DEFAULT_STUCK_IDLE_HOURS

    def test_ticket_with_an_open_task_is_left_alone(self) -> None:
        ticket = _stuck_ticket()
        session = Session.objects.create(ticket=ticket, agent_id="planning")
        Task.objects.create(ticket=ticket, session=session, phase="planning", status=Task.Status.PENDING)

        assert redispatch_stuck_tickets() == 0
        assert ticket.tasks.filter(phase="planning").count() == 1

    def test_recently_active_ticket_is_left_alone(self) -> None:
        ticket = _stuck_ticket(idle_hours=0)

        assert redispatch_stuck_tickets() == 0
        assert ticket.tasks.count() == 0

    def test_terminal_ticket_is_left_alone(self) -> None:
        ticket = _stuck_ticket(state=Ticket.State.MERGED)

        assert redispatch_stuck_tickets() == 0
        assert ticket.tasks.count() == 0

    def test_ticket_with_an_open_pr_is_left_alone(self) -> None:
        ticket = _stuck_ticket()
        ticket.pull_requests.create(url="https://ex.com/pr/1", repo="acme/app", iid="1")

        assert redispatch_stuck_tickets() == 0
        assert ticket.tasks.count() == 0

    def test_budget_exhausted_ticket_is_escalated_not_scheduled(self) -> None:
        ticket = _stuck_ticket(state=Ticket.State.STARTED)
        cap = max_phase_iterations()
        # A run of prior distinct planning-phase failures burns the repair budget.
        for i in range(cap):
            session = Session.objects.create(ticket=ticket, agent_id="planning")
            task = Task.objects.create(ticket=ticket, session=session, phase="planning", status=Task.Status.FAILED)
            attempt = TaskAttempt.objects.create(
                task=task,
                execution_target=task.execution_target,
                ended_at=timezone.now(),
                exit_code=1,
                error=f"planning failed run {'x' * (i + 1)}",
            )
            # The failures are stale — the ticket has been sitting since they ran.
            TaskAttempt.objects.filter(pk=attempt.pk).update(started_at=timezone.now() - timedelta(hours=48))

        scheduled = redispatch_stuck_tickets()

        assert scheduled == 0
        assert ticket.tasks.filter(phase="planning", status=Task.Status.PENDING).count() == 0
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 1
        # Idempotent: a second sweep neither schedules nor spams another escalation.
        assert redispatch_stuck_tickets() == 0
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 1

    def _burn_planning_budget(self, ticket: Ticket) -> None:
        for i in range(max_phase_iterations()):
            session = Session.objects.create(ticket=ticket, agent_id="planning")
            task = Task.objects.create(ticket=ticket, session=session, phase="planning", status=Task.Status.FAILED)
            attempt = TaskAttempt.objects.create(
                task=task,
                execution_target=task.execution_target,
                ended_at=timezone.now(),
                exit_code=1,
                error=f"planning failed run {'x' * (i + 1)}",
            )
            TaskAttempt.objects.filter(pk=attempt.pk).update(started_at=timezone.now() - timedelta(hours=48))

    def test_answered_escalation_does_not_re_escalate(self) -> None:
        # #6: an escalated stuck ticket is parked. Answering the question must NOT spawn
        # a fresh escalation on the next tick (the old open-only dedup re-fired here).
        ticket = _stuck_ticket(state=Ticket.State.STARTED)
        self._burn_planning_budget(ticket)
        assert redispatch_stuck_tickets() == 0
        question = DeferredQuestion.objects.get()
        question.answered_at = timezone.now()
        question.save(update_fields=["answered_at"])

        assert redispatch_stuck_tickets() == 0
        assert DeferredQuestion.objects.count() == 1  # no re-escalation after the answer

    def test_wired_into_tick_recovery(self) -> None:
        ticket = _stuck_ticket(state=Ticket.State.STARTED)

        _reap_stale_task_claims()

        assert ticket.tasks.filter(phase="planning", status=Task.Status.PENDING).count() == 1
