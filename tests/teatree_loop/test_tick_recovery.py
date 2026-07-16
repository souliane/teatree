"""Boot/tick recovery step wiring — the housekeeping sweeps run every tick.

``_reap_stale_task_claims`` chains the boot sweeps, the transient auto-requeue,
and the stuck-ticket re-dispatch so a returned-failure task and a frozen ticket
both self-heal from the loop tick, never only from an explicit ``t3 recover``.
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import Session, Task, TaskAttempt, Ticket
from teatree.core.models.transition import TicketTransition
from teatree.loop.tick_recovery import _reap_stale_task_claims


class TestReapStaleTaskClaims(TestCase):
    def test_runs_the_transient_requeue_and_the_stuck_redispatch(self) -> None:
        transient = self._transient_failed_task()
        stuck = self._stuck_started_ticket()

        _reap_stale_task_claims()

        transient.refresh_from_db()
        assert transient.status == Task.Status.PENDING
        assert stuck.tasks.filter(phase="planning", status=Task.Status.PENDING).count() == 1

    def _transient_failed_task(self) -> Task:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="coding")
        task = Task.objects.create(ticket=ticket, session=session, phase="coding", status=Task.Status.FAILED)
        TaskAttempt.objects.create(
            task=task,
            execution_target=task.execution_target,
            ended_at=timezone.now(),
            exit_code=1,
            error="outage_death: connection refused",
        )
        return task

    def _stuck_started_ticket(self) -> Ticket:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.STARTED)
        transition = TicketTransition.objects.create(ticket=ticket, from_state="scoped", to_state="started")
        TicketTransition.objects.filter(pk=transition.pk).update(created_at=timezone.now() - timedelta(hours=48))
        return ticket
