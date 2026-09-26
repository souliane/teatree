"""The queue invariant is independent of the admission brake's cause."""

import datetime as dt
import io
from contextlib import redirect_stdout
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.cli.doctor.checks_admission_pressure import _check_queue_stall
from teatree.core.models import Session, Task, Ticket


class QueueStallCheckTests(TestCase):
    def setUp(self) -> None:
        from django.db.models.signals import post_save  # noqa: PLC0415

        from teatree.core.signals import _auto_enqueue_task  # noqa: PLC0415

        post_save.disconnect(_auto_enqueue_task, sender=Task, dispatch_uid="auto_enqueue_task")
        self.addCleanup(post_save.connect, _auto_enqueue_task, sender=Task, dispatch_uid="auto_enqueue_task")
        ticket = Ticket.objects.create(role="author")
        self.session = Session.objects.create(ticket=ticket)
        self.ticket = ticket

    def _task(self, *, age_minutes: int, status: str = Task.Status.PENDING) -> Task:
        task = Task.objects.create(ticket=self.ticket, session=self.session, status=status, phase="coding")
        Task.objects.filter(pk=task.pk).update(created_at=timezone.now() - dt.timedelta(minutes=age_minutes))
        return task

    def test_old_unclaimed_queue_fails_with_latest_admission_reason(self) -> None:
        self._task(age_minutes=35)
        with patch(
            "teatree.core.telemetry.admission.latest_admission_reason",
            return_value="weekly window exhausted",
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                assert _check_queue_stall() is False
        assert "FAIL" in output.getvalue()
        assert "weekly window exhausted" in output.getvalue()
        assert "1 pending" in output.getvalue()

    def test_recent_claim_prevents_one_old_pending_row_from_firing(self) -> None:
        self._task(age_minutes=35)
        claimed = self._task(age_minutes=36, status=Task.Status.CLAIMED)
        Task.objects.filter(pk=claimed.pk).update(claimed_at=timezone.now() - dt.timedelta(minutes=2))
        assert _check_queue_stall() is True

    def test_recently_created_queue_is_healthy(self) -> None:
        self._task(age_minutes=5)
        assert _check_queue_stall() is True

    def test_empty_queue_is_healthy(self) -> None:
        assert _check_queue_stall() is True

    def test_threshold_is_configurable(self) -> None:
        self._task(age_minutes=12)
        with patch.dict("os.environ", {"TEATREE_QUEUE_STALL_MINUTES": "10"}):
            assert _check_queue_stall() is False
