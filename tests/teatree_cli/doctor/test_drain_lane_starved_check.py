"""``_check_drain_lane_starved`` — the doctor's drain-starvation surface (#4374).

Expensive work held every slot for over half an hour with five reviewing rows queued and
zero reviews running, and no surface anywhere said so: the worker was busy, the loop
ticked, nothing errored. This check names the state while it is still happening.
"""

import datetime as dt
import io
from contextlib import redirect_stdout
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.cli.doctor.checks_admission_pressure import _check_drain_lane_starved
from teatree.core.factory.drain_starvation import DRAIN_STARVED_AFTER
from teatree.core.models import Session, Task, Ticket


class DrainLaneStarvedDoctorCheckTests(TestCase):
    def setUp(self) -> None:
        from django.db.models.signals import post_save  # noqa: PLC0415 - deferred: local import

        from teatree.core.signals import _auto_enqueue_task  # noqa: PLC0415 - deferred: local import

        post_save.disconnect(_auto_enqueue_task, sender=Task, dispatch_uid="auto_enqueue_task")
        self.addCleanup(post_save.connect, _auto_enqueue_task, sender=Task, dispatch_uid="auto_enqueue_task")
        self.ticket = Ticket.objects.create(role="author")

    def _queued_review(self, *, waited: dt.timedelta) -> None:
        task = Task.objects.create(
            ticket=self.ticket,
            session=Session.objects.create(ticket=self.ticket),
            status=Task.Status.PENDING,
            phase="reviewing",
        )
        Task.objects.filter(pk=task.pk).update(created_at=timezone.now() - waited)

    def test_an_idle_lane_is_ok(self) -> None:
        assert _check_drain_lane_starved() is True

    def test_a_queue_inside_the_threshold_is_ok(self) -> None:
        self._queued_review(waited=DRAIN_STARVED_AFTER / 2)

        assert _check_drain_lane_starved() is True

    def test_a_starved_lane_warns(self) -> None:
        self._queued_review(waited=DRAIN_STARVED_AFTER * 2)

        assert _check_drain_lane_starved() is False

    def test_the_warning_names_the_depth_the_wait_and_the_remedy(self) -> None:
        self._queued_review(waited=dt.timedelta(minutes=34))

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _check_drain_lane_starved()
        output = buffer.getvalue()

        assert "1 reviewing/shipping task(s) queued" in output
        assert "oldest waiting 34m" in output
        assert "drain_slot_reservation" in output

    def test_a_crashed_read_degrades_to_ok(self) -> None:
        # An advisory that cannot read its own state must never redden the doctor run.
        with patch("teatree.core.factory.drain_starvation.read_drain_lane_state", side_effect=RuntimeError("db gone")):
            assert _check_drain_lane_starved() is True
