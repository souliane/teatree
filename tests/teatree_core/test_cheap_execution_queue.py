"""A review gets its own executable queue, independent of long coding jobs."""

from django.test import TestCase, override_settings
from django_tasks_db.models import DBTaskResult

from teatree.core.task_dispatch import enqueue_execution

_DB_TASKS = {"default": {"BACKEND": "django_tasks_db.DatabaseBackend", "QUEUES": ["default", "loops", "cheap"]}}


@override_settings(TASKS=_DB_TASKS)
class TestCheapExecutionQueue(TestCase):
    def test_review_and_shipping_use_the_drain_queue(self) -> None:
        enqueue_execution(1, "reviewing")
        enqueue_execution(2, "shipping")
        assert list(DBTaskResult.objects.order_by("enqueued_at").values_list("queue_name", flat=True)) == [
            "cheap",
            "cheap",
        ]

    def test_coding_remains_on_default_queue(self) -> None:
        enqueue_execution(1, "coding")
        assert DBTaskResult.objects.get().queue_name == "default"
