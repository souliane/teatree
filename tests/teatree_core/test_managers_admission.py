"""The task admission seat predicates partition live work by phase cost."""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.core.managers_admission import ADMITTED_INFLIGHT_WINDOW, _lane_occupancy_q
from teatree.core.models import Session, Task, Ticket


class TestAdmissionSeatPredicates(TestCase):
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
