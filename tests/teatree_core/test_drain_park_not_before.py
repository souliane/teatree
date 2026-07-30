"""The headless drain honours ``not_before`` — a window-parked task is skipped (F5).

A usage-limit-parked task is PENDING with a future ``not_before``. Before the fix the
drain filtered on ``status=PENDING`` only, re-enqueuing the parked task every ~5 min so
the runner pre-flight re-parked it — a junk park attempt each cycle. The drain now shares
``_claimable_now_q`` with the claim path, so a parked task is left queued until its window.
"""

import datetime as dt
from unittest.mock import patch

from django.db.models.signals import post_save
from django.test import TestCase, override_settings
from django.utils import timezone

import teatree.core.overlay_loader as overlay_loader_mod
from teatree.core.models import Session, Task, Ticket
from teatree.core.signals import _auto_enqueue_headless_task
from teatree.core.tasks import drain_headless_queue_body
from tests.teatree_core.conftest import CommandOverlay

IMMEDIATE_BACKEND = {"TASKS": {"default": {"BACKEND": "django_tasks.backends.immediate.ImmediateBackend"}}}
_MOCK_OVERLAY = {"test": CommandOverlay()}


class TestDrainHonoursNotBefore(TestCase):
    def setUp(self) -> None:
        uid = "auto_enqueue_headless"
        post_save.disconnect(_auto_enqueue_headless_task, sender=Task, dispatch_uid=uid)
        self.addCleanup(post_save.connect, _auto_enqueue_headless_task, sender=Task, dispatch_uid=uid)
        self.ticket = Ticket.objects.create(overlay="test")
        self.session = Session.objects.create(ticket=self.ticket, overlay="test")

    def _pending(self, *, not_before: dt.datetime | None) -> Task:
        return Task.objects.create(
            ticket=self.ticket,
            session=self.session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.PENDING,
            phase="architectural_review",
            not_before=not_before,
        )

    @override_settings(**IMMEDIATE_BACKEND)
    def test_window_parked_task_is_not_enqueued(self) -> None:
        parked = self._pending(not_before=timezone.now() + dt.timedelta(hours=4))
        with patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY):
            result = drain_headless_queue_body()
        assert parked.pk not in result["enqueued"]
        parked.refresh_from_db()
        assert parked.status == Task.Status.PENDING  # still queued, never claimed/parked again

    @override_settings(**IMMEDIATE_BACKEND)
    def test_elapsed_not_before_task_is_enqueued(self) -> None:
        # Control: a task whose park window has re-armed drains normally, so the filter
        # gates on the future ``not_before`` alone, not on the field being present.
        ready = self._pending(not_before=timezone.now() - dt.timedelta(minutes=1))
        with (
            patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.tasks.execute_headless_task") as mock_task,
        ):
            result = drain_headless_queue_body()
        assert result["enqueued"] == [ready.pk]
        mock_task.enqueue.assert_called_once_with(ready.pk, ready.phase)
