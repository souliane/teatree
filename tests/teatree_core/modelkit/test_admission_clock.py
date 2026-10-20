from datetime import timedelta

from django.db.models.signals import post_save
from django.test import TestCase
from django.utils import timezone

from teatree.core.modelkit.admission_clock import earliest_admission_stamp
from teatree.core.models import Session, Task, Ticket
from teatree.core.signals import _auto_enqueue_task


class TestEarliestAdmissionStamp(TestCase):
    def setUp(self) -> None:
        post_save.disconnect(_auto_enqueue_task, sender=Task, dispatch_uid="auto_enqueue_task")
        self.addCleanup(post_save.connect, _auto_enqueue_task, sender=Task, dispatch_uid="auto_enqueue_task")

    def test_earlier_renewable_seat_wins_over_later_first_stamp(self) -> None:
        ticket = Ticket.objects.create()
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")
        now = timezone.now()
        Task.objects.filter(pk=task.pk).update(
            admitted_at=now - timedelta(hours=8), first_admitted_at=now - timedelta(hours=1)
        )

        stamp = (
            Task.objects.filter(pk=task.pk)
            .annotate(earliest=earliest_admission_stamp())
            .values_list("earliest", flat=True)
            .get()
        )

        assert stamp == now - timedelta(hours=8)

    def test_a_null_stamp_uses_the_other_and_two_nulls_stay_unmeasured(self) -> None:
        ticket = Ticket.objects.create()
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")
        now = timezone.now()
        stamps = Task.objects.filter(pk=task.pk).annotate(earliest=earliest_admission_stamp())
        assert stamps.values_list("earliest", flat=True).get() is None
        Task.objects.filter(pk=task.pk).update(first_admitted_at=now)
        assert stamps.values_list("earliest", flat=True).get() == now
