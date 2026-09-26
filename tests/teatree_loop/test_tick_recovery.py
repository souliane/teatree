"""Boot/tick recovery step wiring — the housekeeping sweeps run every tick.

``_reap_stale_task_claims`` chains the boot sweeps, the transient auto-requeue,
and the stuck-ticket re-dispatch so a returned-failure task and a frozen ticket
both self-heal from the loop tick, never only from an explicit ``t3 recover``.
"""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

from teatree.core.gates.plan_dispatch_gate import PLAN_MISSING_PREFIX
from teatree.core.models import ModeOverride, Session, Task, TaskAttempt, Ticket
from teatree.core.models.transition import TicketTransition
from teatree.core.tasks import drain_queue_body
from teatree.loop.tick_recovery import _reap_stale_task_claims

_DB_TASKS = {"default": {"BACKEND": "django_tasks_db.DatabaseBackend", "QUEUES": ["default", "loops", "cheap"]}}


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

    def test_a_stop_after_the_recovery_probe_re_dispatches_nothing_and_lifting_it_recovers_once(self) -> None:
        transient = self._transient_failed_task()
        stuck = self._stuck_started_ticket()
        unplanned = self._plan_refused_ticket()

        def the_fleet_stops_after_the_probe(_errors: object) -> str:
            ModeOverride.objects.set_override("off", reason="test: the owner stopped the fleet mid-recovery")
            return ""

        with patch("teatree.loop.tick_recovery._redispatch_block_reason", the_fleet_stops_after_the_probe):
            _reap_stale_task_claims()

        transient.refresh_from_db()
        unplanned.refresh_from_db()
        assert transient.status == Task.Status.FAILED
        assert not stuck.tasks.exists()
        assert unplanned.state == Ticket.State.NOT_STARTED
        assert not unplanned.tasks.filter(phase="planning").exists()

        ModeOverride.objects.all().delete()
        _reap_stale_task_claims()
        _reap_stale_task_claims()

        transient.refresh_from_db()
        assert transient.status == Task.Status.PENDING
        assert stuck.tasks.filter(phase="planning").count() == 1
        assert unplanned.tasks.filter(phase="planning").count() == 1

    def _plan_refused_ticket(self) -> Ticket:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.NOT_STARTED, overlay="acme")
        task = Task.objects.create(
            ticket=ticket, session=Session.objects.create(ticket=ticket, agent_id="coding"), phase="coding"
        )
        task.fail(reason=f"{PLAN_MISSING_PREFIX}refusing to dispatch t3:coder for ticket {ticket.pk} (coding)")
        return ticket

    def _transient_failed_task(self) -> Task:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.WORK_STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="coding")
        task = Task.objects.create(ticket=ticket, session=session, phase="coding", status=Task.Status.FAILED)
        TaskAttempt.objects.create(
            task=task,
            ended_at=timezone.now(),
            exit_code=1,
            error="outage_death: connection refused",
        )
        return task

    def _stuck_started_ticket(self) -> Ticket:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.WORK_STARTED)
        transition = TicketTransition.objects.create(ticket=ticket, from_state="scoped", to_state="work_started")
        TicketTransition.objects.filter(pk=transition.pk).update(created_at=timezone.now() - timedelta(hours=48))
        return ticket

    def test_the_off_posture_redispatches_no_work(self) -> None:
        transient = self._transient_failed_task()
        stuck = self._stuck_started_ticket()
        ModeOverride.objects.set_override("off", reason="test: the operator stopped the fleet")

        _reap_stale_task_claims()

        transient.refresh_from_db()
        assert transient.status == Task.Status.FAILED
        assert not stuck.tasks.exists()


@override_settings(TASKS=_DB_TASKS)
class TestTheOffPostureAcrossTicks(TestCase):
    def test_ten_ticks_under_off_queue_nothing_then_lifting_re_admits_the_backlog(self) -> None:
        ModeOverride.objects.set_override("off", reason="test: the operator stopped the fleet")
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.WORK_STARTED)
        queued = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")
        stuck = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.WORK_STARTED)
        transition = TicketTransition.objects.create(ticket=stuck, from_state="scoped", to_state="work_started")
        TicketTransition.objects.filter(pk=transition.pk).update(created_at=timezone.now() - timedelta(hours=48))

        for _ in range(10):
            _reap_stale_task_claims()
            drain_queue_body()

        assert DBTaskResult.objects.count() == 0
        assert not TaskAttempt.objects.exists()
        assert list(Task.objects.values_list("pk", flat=True)) == [queued.pk]

        ModeOverride.objects.all().delete()

        assert drain_queue_body()["enqueued"] == [queued.pk]
        assert DBTaskResult.objects.count() == 1


class TestAnUnreadableAdmissionVerdictHoldsReDispatch(TestCase):
    def test_re_dispatch_waits_and_the_read_failure_is_recorded(self) -> None:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.WORK_STARTED)
        transient = Task.objects.create(
            ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding", status=Task.Status.FAILED
        )
        TaskAttempt.objects.create(task=transient, ended_at=timezone.now(), exit_code=1, error="outage_death: refused")
        errors: dict[str, str] = {}

        with patch("teatree.core.managers.claim_admission_block_reason", side_effect=RuntimeError("settings locked")):
            _reap_stale_task_claims(errors)

        transient.refresh_from_db()
        assert transient.status == Task.Status.FAILED
        assert errors == {"recovery:admission": "RuntimeError: settings locked"}
