"""The drain-lane starvation probe — the stall's own signature, made legible (#4374).

Three coding agents held every slot for over half an hour with five reviewing rows queued
and zero reviews running, and nothing anywhere named it: the worker was busy, the loop
ticked, no error was raised. These pin that the probe fires on exactly that state and on
nothing that merely resembles it.
"""

import datetime as dt

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.factory.drain_starvation import DRAIN_STARVED_AFTER, read_drain_lane_state
from teatree.core.models import Session, Task, Ticket

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


class DrainLaneStateTests(TestCase):
    def setUp(self) -> None:
        from django.db.models.signals import post_save  # noqa: PLC0415 - deferred: local import

        from teatree.core.signals import _auto_enqueue_task  # noqa: PLC0415 - deferred: local import

        post_save.disconnect(_auto_enqueue_task, sender=Task, dispatch_uid="auto_enqueue_task")
        self.addCleanup(post_save.connect, _auto_enqueue_task, sender=Task, dispatch_uid="auto_enqueue_task")
        self.ticket = Ticket.objects.create(role="author")

    def _task(self, phase: str, *, status: str, waited: dt.timedelta | None = None) -> Task:
        now = timezone.now()
        task = Task.objects.create(
            ticket=self.ticket,
            session=Session.objects.create(ticket=self.ticket),
            status=status,
            phase=phase,
            lease_expires_at=now + dt.timedelta(hours=1) if status == Task.Status.CLAIMED else None,
        )
        if waited is not None:
            Task.objects.filter(pk=task.pk).update(created_at=now - waited)
        return task

    def test_an_idle_queue_is_not_starved(self) -> None:
        assert read_drain_lane_state().starved is False

    def test_a_queue_that_has_only_just_formed_is_not_starved(self) -> None:
        # Momentarily true every time one review ends before the next starts.
        self._task("reviewing", status=Task.Status.PENDING, waited=DRAIN_STARVED_AFTER / 2)

        assert read_drain_lane_state().starved is False

    def test_draining_work_queued_with_none_running_past_the_threshold_is_starved(self) -> None:
        self._task("reviewing", status=Task.Status.PENDING, waited=DRAIN_STARVED_AFTER * 2)

        state = read_drain_lane_state()

        assert state.starved is True
        assert state.pending == 1
        assert state.running == 0

    def test_a_deep_queue_that_is_moving_is_not_starved(self) -> None:
        # A backlog behind a live review is throughput, not starvation.
        for _ in range(5):
            self._task("reviewing", status=Task.Status.PENDING, waited=DRAIN_STARVED_AFTER * 2)
        self._task("shipping", status=Task.Status.CLAIMED)

        assert read_drain_lane_state().starved is False

    def test_expensive_work_running_does_not_count_as_the_lane_moving(self) -> None:
        # The headline shape: coding agents hold every slot, which is precisely why the
        # draining class cannot get in — counting them as progress would hide the stall.
        self._task("reviewing", status=Task.Status.PENDING, waited=DRAIN_STARVED_AFTER * 2)
        for _ in range(3):
            self._task("coding", status=Task.Status.CLAIMED)

        assert read_drain_lane_state().starved is True

    def test_a_queued_row_no_subagent_routes_is_not_counted(self) -> None:
        # No capacity can relieve a row nothing will ever dispatch, so reporting it would
        # be an alarm with no action behind it.
        self.ticket.role = "nobody"
        self.ticket.save(update_fields=["role"])
        self._task("reviewing", status=Task.Status.PENDING, waited=DRAIN_STARVED_AFTER * 2)

        state = read_drain_lane_state()

        assert state.pending == 0
        assert state.starved is False

    def test_an_expired_lease_is_not_a_running_agent(self) -> None:
        # A claim whose owner stopped heartbeating is not draining anything; treating it
        # as live would silence the alarm for exactly as long as the wedge lasts.
        self._task("reviewing", status=Task.Status.PENDING, waited=DRAIN_STARVED_AFTER * 2)
        dead = self._task("reviewing", status=Task.Status.CLAIMED)
        Task.objects.filter(pk=dead.pk).update(lease_expires_at=timezone.now() - dt.timedelta(hours=1))

        assert read_drain_lane_state().starved is True

    def test_the_report_names_the_depth_and_the_wait(self) -> None:
        self._task("reviewing", status=Task.Status.PENDING, waited=dt.timedelta(minutes=34))
        self._task("shipping", status=Task.Status.PENDING, waited=dt.timedelta(minutes=5))

        report = read_drain_lane_state().report()

        assert "2 reviewing/shipping task(s) queued" in report
        assert "oldest waiting 34m" in report
