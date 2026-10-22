from datetime import UTC, datetime, timedelta

from django.test import TestCase

from teatree.core.models.task_cancel_source import cancel_source, count_recorded_cancels
from tests.factories import TaskAttemptFactory, TaskFactory


class CancelSourceTests(TestCase):
    def test_latest_cancel_audit_wins(self) -> None:
        task = TaskFactory()
        assert cancel_source(task) == ""
        TaskAttemptFactory(task=task, result={"cancel_source": "owner"})
        TaskAttemptFactory(task=task, result={"summary": "interrupted"})
        TaskAttemptFactory(task=task, result={"cancel_source": "agent"})
        assert cancel_source(task) == "agent"

    def test_batch_count_stays_within_the_merge_window(self) -> None:
        start = datetime(2026, 10, 17, tzinfo=UTC)
        rows = (
            (start + timedelta(minutes=5), {"cancel_source": "owner"}),
            (start + timedelta(minutes=10), {"cancel_source": "agent"}),
            (start + timedelta(minutes=15), {"summary": "interrupted"}),
            (start + timedelta(minutes=31), {"cancel_source": "owner"}),
            (None, {"cancel_source": "owner"}),
        )
        assert count_recorded_cancels(rows, start=start, merged_at=start + timedelta(minutes=30)) == 2
        assert count_recorded_cancels(rows, start=None, merged_at=start + timedelta(minutes=30)) == 0
