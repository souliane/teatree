"""Boot/tick recovery step wiring — the housekeeping sweeps run every tick.

``_reap_stale_task_claims`` chains the boot sweeps, the transient auto-requeue,
and the stuck-ticket re-dispatch so a returned-failure task and a frozen ticket
both self-heal from the loop tick, never only from an explicit ``t3 recover``.
"""

from datetime import timedelta
from unittest.mock import patch

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

    def test_a_failing_boot_sweep_does_not_skip_the_other_two_and_is_recorded(self) -> None:
        # #5: the old shared suppress(RuntimeError) let the FIRST sweep's failure skip
        # BOTH later sweeps AND vanish. Each sweep must run independently, and a failure
        # must land in the errors sink (rendered in action_needed), never silently.
        transient = self._transient_failed_task()
        stuck = self._stuck_started_ticket()
        errors: dict[str, str] = {}

        with patch(
            "teatree.core.worktree.recovery_sweeps.run_boot_sweeps",
            side_effect=RuntimeError("boot sweep exploded"),
        ):
            _reap_stale_task_claims(errors)

        # The boot-sweep failure was recorded, not swallowed...
        assert "recovery:boot_sweeps" in errors
        assert "boot sweep exploded" in errors["recovery:boot_sweeps"]
        # ...and the other two sweeps still ran despite it.
        transient.refresh_from_db()
        assert transient.status == Task.Status.PENDING
        assert stuck.tasks.filter(phase="planning", status=Task.Status.PENDING).count() == 1

    def test_a_non_runtimeerror_sweep_failure_is_isolated_and_recorded(self) -> None:
        # #3441: a sweep can raise more than RuntimeError (a DatabaseError on a poison
        # row, a ValueError from a classifier). The old ``except RuntimeError`` let such
        # an exception abort the whole recovery step; the broadened ``except Exception``
        # must isolate it — record it in the errors sink AND still run the later sweeps.
        transient = self._transient_failed_task()
        stuck = self._stuck_started_ticket()
        errors: dict[str, str] = {}

        with patch(
            "teatree.core.worktree.recovery_sweeps.run_boot_sweeps",
            side_effect=ValueError("boot sweep hit a poison row"),
        ):
            _reap_stale_task_claims(errors)

        # The non-RuntimeError failure was recorded, not propagated...
        assert "recovery:boot_sweeps" in errors
        assert "ValueError" in errors["recovery:boot_sweeps"]
        # ...and the other two sweeps still ran despite it.
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
