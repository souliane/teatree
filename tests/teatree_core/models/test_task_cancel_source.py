from django.test import TestCase

from teatree.core.models.task_cancel_source import cancel_source
from tests.factories import TaskAttemptFactory, TaskFactory


class CancelSourceTests(TestCase):
    def test_latest_cancel_audit_wins(self) -> None:
        task = TaskFactory()
        assert cancel_source(task) == ""
        TaskAttemptFactory(task=task, result={"cancel_source": "owner"})
        TaskAttemptFactory(task=task, result={"summary": "interrupted"})
        TaskAttemptFactory(task=task, result={"cancel_source": "agent"})
        assert cancel_source(task) == "agent"
