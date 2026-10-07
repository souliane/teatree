"""One ranked admission walk serves both the post_save receiver and the queue drain (#5051).

Admission used to book the row being saved whenever its lane had room, so a freshly
created task overtook every older row already waiting in the same lane, and a freed seat
sat empty until the next five-minute drain. These drive the real receiver and the real
drain; only the governor's box and quota probes are pinned.
"""

import datetime as dt
from unittest.mock import patch

from django.db.models.signals import post_save
from django.test import TestCase
from django.utils import timezone

from teatree.core import agent_admission as gate_mod
from teatree.core import task_dispatch as task_dispatch_mod
from teatree.core.models import ModeOverride, Session, Task, Ticket
from teatree.core.signals import _auto_enqueue_task
from teatree.core.task_dispatch import admit_waiting_tasks
from teatree.core.tasks import drain_queue_body
from tests.teatree_core.conftest import HEALTHY_MACHINE_SIGNAL, HEALTHY_QUOTA_SIGNAL

#: The governor ceiling the pinned 8-core healthy box derives.
_CODING_CEILING = 4
#: The shipped ``cheap_phase_admission_ceiling``.
_REVIEW_WIDTH = 2


class _AdmissionCase(TestCase):
    def setUp(self) -> None:
        for name, signal in (
            ("read_quota_signal", HEALTHY_QUOTA_SIGNAL),
            ("read_machine_signal", HEALTHY_MACHINE_SIGNAL),
        ):
            probe = patch.object(gate_mod, name, return_value=signal)
            probe.start()
            self.addCleanup(probe.stop)
        runner = patch.object(task_dispatch_mod, "execute_task")
        runner.start()
        self.addCleanup(runner.stop)
        self.ticket = Ticket.objects.create()

    def _task(self, phase: str, *, status: str = Task.Status.PENDING, ticket: Ticket | None = None) -> Task:
        owner = ticket or self.ticket
        return Task.objects.create(
            ticket=owner,
            session=Session.objects.create(ticket=owner),
            status=status,
            phase=phase,
            lease_expires_at=timezone.now() + dt.timedelta(hours=1) if status == Task.Status.CLAIMED else None,
        )

    def _fill_the_review_lane(self) -> list[Task]:
        return [self._task("reviewing", status=Task.Status.CLAIMED) for _ in range(_REVIEW_WIDTH)]

    @staticmethod
    def _admitted(task: Task) -> bool:
        task.refresh_from_db()
        return task.admitted_at is not None


class TestACreatedRowDoesNotOvertakeAnOlderWaiter(_AdmissionCase):
    def test_the_older_waiter_takes_the_freed_seat_not_the_new_row(self) -> None:
        running = self._fill_the_review_lane()
        older = self._task("reviewing")
        Task.objects.filter(pk=running[0].pk).update(status=Task.Status.COMPLETED)

        newer = self._task("critic_reviewing")

        assert self._admitted(older)
        assert not self._admitted(newer)


class TestAWalkSkipsRowsAlreadyDispatched(_AdmissionCase):
    def test_the_next_walk_neither_rebooks_nor_reports_a_seated_row(self) -> None:
        first = self._task("reviewing")
        assert self._admitted(first)

        with self.assertNoLogs("teatree.core.agent_admission", level="WARNING"):
            self._task("reviewing")


class TestAFreedSeatIsRefilledInTheSameSave(_AdmissionCase):
    def test_a_task_reaching_a_terminal_state_admits_the_oldest_waiter(self) -> None:
        running = self._fill_the_review_lane()
        waiting = self._task("reviewing")
        assert not self._admitted(waiting)

        running[0].fail(reason="agent exited", by_holder=True)

        assert self._admitted(waiting)


class TestTheDrainAdmitsExpeditedWorkFirst(_AdmissionCase):
    def setUp(self) -> None:
        super().setUp()
        post_save.disconnect(_auto_enqueue_task, sender=Task, dispatch_uid="auto_enqueue_task")
        self.addCleanup(post_save.connect, _auto_enqueue_task, sender=Task, dispatch_uid="auto_enqueue_task")

    def _one_free_seat_two_waiters(self, *, expedited: bool) -> tuple[Task, Task]:
        for _ in range(_CODING_CEILING - 1):
            self._task("coding", status=Task.Status.CLAIMED)
        older = self._task("coding")
        newer = self._task("planning", ticket=Ticket.objects.create(expedited=expedited))
        return older, newer

    def test_a_newer_expedited_row_takes_the_last_seat(self) -> None:
        older, newer = self._one_free_seat_two_waiters(expedited=True)

        assert drain_queue_body()["enqueued"] == [newer.pk]
        assert not self._admitted(older)

    def test_control_without_the_flag_the_older_row_goes_first(self) -> None:
        older, newer = self._one_free_seat_two_waiters(expedited=False)

        assert drain_queue_body()["enqueued"] == [older.pk]
        assert not self._admitted(newer)


class TestAFrozenFactoryAdmitsNothing(_AdmissionCase):
    def setUp(self) -> None:
        super().setUp()
        post_save.disconnect(_auto_enqueue_task, sender=Task, dispatch_uid="auto_enqueue_task")
        self.addCleanup(post_save.connect, _auto_enqueue_task, sender=Task, dispatch_uid="auto_enqueue_task")

    def test_the_off_posture_withholds_every_waiting_row(self) -> None:
        waiting = self._task("reviewing")
        ModeOverride.objects.set_override("off", reason="test: the operator stopped the fleet")

        assert admit_waiting_tasks(at="test") == []
        assert not self._admitted(waiting)

    def test_control_an_admitting_fleet_admits_the_same_row(self) -> None:
        waiting = self._task("reviewing")

        assert admit_waiting_tasks(at="test") == [waiting.pk]
