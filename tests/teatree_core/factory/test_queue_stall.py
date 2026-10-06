"""The shared queue-stall predicate the doctor check and the dispatch-gap detector both read."""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.core.factory.queue_stall import DEFAULT_STALL_MINUTES, read_queue_stall, stall_minutes
from teatree.core.models import Session, Task, Ticket


class StallMinutesTests(TestCase):
    def test_default(self) -> None:
        with patch.dict("os.environ", {}, clear=False) as env:
            env.pop("TEATREE_QUEUE_STALL_MINUTES", None)
            assert stall_minutes() == DEFAULT_STALL_MINUTES

    def test_override_within_bounds(self) -> None:
        with patch.dict("os.environ", {"TEATREE_QUEUE_STALL_MINUTES": "12"}):
            assert stall_minutes() == 12

    def test_invalid_or_out_of_range_override_falls_back(self) -> None:
        for raw in ("soon", "0", str(24 * 60 + 1)):
            with patch.dict("os.environ", {"TEATREE_QUEUE_STALL_MINUTES": raw}):
                assert stall_minutes() == DEFAULT_STALL_MINUTES


class ReadQueueStallTests(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(overlay="acme")
        self.session = Session.objects.create(ticket=self.ticket)

    def _task(self, age_minutes: int) -> Task:
        task = Task.objects.create(ticket=self.ticket, session=self.session, phase="coding")
        Task.objects.filter(pk=task.pk).update(created_at=timezone.now() - timedelta(minutes=age_minutes))
        return task

    def test_names_the_oldest_row_and_counts_the_set(self) -> None:
        oldest = self._task(age_minutes=50)
        self._task(age_minutes=40)

        stall = read_queue_stall(Task.objects.filter(status=Task.Status.PENDING), now=timezone.now(), minutes=30)

        assert stall is not None
        assert stall.pending == 2
        assert stall.oldest_pk == oldest.pk

    def test_empty_set_is_no_stall(self) -> None:
        assert read_queue_stall(Task.objects.none(), now=timezone.now(), minutes=30) is None
