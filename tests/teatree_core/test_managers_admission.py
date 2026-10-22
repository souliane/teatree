"""The task admission seat predicates partition live work by phase cost."""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

from django.test import TestCase
from django.utils import timezone

from teatree.agents.runner_route_recording import park_after_route_failure
from teatree.agents.usage_window import record_park
from teatree.core.cycle_time.timeline import engagements_for_tickets
from teatree.core.managers_admission import ADMITTED_INFLIGHT_WINDOW, _lane_occupancy_q
from teatree.core.models import Session, Task, TaskAttempt, Ticket
from teatree.core.worktree.checkout_fence import park_task_checkout


class TestAdmissionSeatPredicates(TestCase):
    def test_future_parked_pending_task_releases_its_seat(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        coding = Task.objects.create(ticket=ticket, session=session, phase="coding")
        reviewing = Task.objects.create(ticket=ticket, session=session, phase="reviewing")
        now = timezone.now()
        Task.objects.filter(pk__in=[coding.pk, reviewing.pk]).update(
            status=Task.Status.PENDING,
            admitted_at=now - timedelta(seconds=1),
            not_before=now + timedelta(minutes=30),
        )
        assert Task.objects.expensive_lane_occupancy() == 0
        assert Task.objects.cheap_lane_occupancy() == 0

    def test_a_park_due_now_keeps_its_throttle_seat(self) -> None:
        ticket = Ticket.objects.create()
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")
        Task.objects.filter(pk=task.pk).update(admitted_at=timezone.now() - timedelta(seconds=1))
        task.refresh_from_db()

        task.park(not_before=timezone.now())

        assert Task.objects.get(pk=task.pk).not_before is not None
        assert Task.objects.expensive_lane_occupancy() == 1

    def test_future_park_stays_unseated_when_its_retry_becomes_due(self) -> None:
        ticket = Ticket.objects.create()
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")
        Task.objects.filter(pk=task.pk).update(admitted_at=timezone.now())
        task.refresh_from_db()

        park_task_checkout(task, reason="checkout refused")
        task.refresh_from_db()
        assert task.admitted_at is None
        Task.objects.filter(pk=task.pk).update(not_before=timezone.now() - timedelta(seconds=1))

        assert Task.objects.expensive_lane_occupancy() == 0
        assert Task.objects.waiting_for_admission().filter(pk=task.pk).exists()

    def test_billed_usage_park_keeps_admission_history_for_cycle_time(self) -> None:
        ticket = Ticket.objects.create()
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")
        admitted_at = timezone.now() - timedelta(minutes=2)
        Task.objects.filter(pk=task.pk).update(admitted_at=admitted_at)
        task.refresh_from_db()
        TaskAttempt.objects.create(task=task, ended_at=timezone.now(), exit_code=0, cost_usd=Decimal("0.01"))

        record_park(task, reason="limit_parked: usage window", not_before=timezone.now() + timedelta(days=3))

        task.refresh_from_db()
        assert task.admitted_at == admitted_at
        assert len(engagements_for_tickets([ticket.pk])[ticket.pk, "coding"]) == 1
        assert Task.objects.expensive_lane_occupancy() == 0

    def test_short_route_failure_hold_keeps_readmit_throttle(self) -> None:
        ticket = Ticket.objects.create()
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")
        admitted_at = timezone.now()
        Task.objects.filter(pk=task.pk).update(admitted_at=admitted_at)
        task.refresh_from_db()

        with patch("teatree.agents.runner_route_recording.learn_route_failure"):
            park_after_route_failure(task, Mock(), Mock(hold=timedelta(minutes=1)), phase="coding")

        task.refresh_from_db()
        assert task.admitted_at == admitted_at
        Task.objects.filter(pk=task.pk).update(not_before=timezone.now() - timedelta(seconds=1))
        assert not Task.objects.waiting_for_admission().filter(pk=task.pk).exists()

    def test_a_parked_seat_does_not_refuse_the_conditional_admission_stamp(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        parked = Task.objects.create(ticket=ticket, session=session, phase="coding")
        waiting = Task.objects.create(ticket=ticket, session=session, phase="coding")
        now = timezone.now()
        Task.objects.filter(pk=parked.pk).update(
            status=Task.Status.PENDING, admitted_at=now, not_before=now + timedelta(minutes=30)
        )
        Task.objects.filter(pk=waiting.pk).update(status=Task.Status.PENDING, admitted_at=None)

        assert Task.objects.record_admission(waiting.pk, cheap=False, lane_ceiling=1)

    def test_cost_lanes_exclude_expired_pending_seats(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        reviewing = Task.objects.create(ticket=ticket, session=session, phase="reviewing")
        coding = Task.objects.create(ticket=ticket, session=session, phase="coding")
        stale = Task.objects.create(ticket=ticket, session=session, phase="review")
        now = timezone.now()
        Task.objects.filter(pk=reviewing.pk).update(status=Task.Status.PENDING, admitted_at=now)
        Task.objects.filter(pk=coding.pk).update(status=Task.Status.PENDING, admitted_at=now)
        Task.objects.filter(pk=stale.pk).update(
            status=Task.Status.PENDING,
            admitted_at=now - ADMITTED_INFLIGHT_WINDOW - timedelta(seconds=1),
        )

        assert set(Task.objects.filter(_lane_occupancy_q(now, cheap=True)).values_list("pk", flat=True)) == {
            reviewing.pk
        }
        assert set(Task.objects.filter(_lane_occupancy_q(now, cheap=False)).values_list("pk", flat=True)) == {coding.pk}

    def test_future_park_is_not_reported_as_a_released_cheap_seat(self) -> None:
        ticket = Ticket.objects.create()
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="reviewing")
        Task.objects.filter(pk=task.pk).update(
            admitted_at=timezone.now() - ADMITTED_INFLIGHT_WINDOW - timedelta(seconds=1),
            not_before=timezone.now() + timedelta(minutes=30),
        )
        assert Task.objects.cheap_lane_seats_released() == 0

        Task.objects.filter(pk=task.pk).update(not_before=timezone.now() - timedelta(seconds=1))
        assert Task.objects.cheap_lane_seats_released() == 1
