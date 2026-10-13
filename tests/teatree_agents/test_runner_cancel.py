from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from teatree.agents.runner_cancel import settle_cancelled_checkout
from teatree.core.modelkit.task_failure_taxonomy import FailureKind
from teatree.core.models import Task
from tests.factories import TaskAttemptFactory, TaskFactory


class RunnerCancelTests(TestCase):
    def test_active_run_does_not_wait_or_signal(self) -> None:
        task = TaskFactory()
        with patch("teatree.agents.runner_cancel.wait_for_owned_checkout_processes") as wait:
            settle_cancelled_checkout(task, Path("/checkout"), beat_seconds=0)
        wait.assert_not_called()

    def test_agent_cancel_waits_but_never_signals(self) -> None:
        task = TaskFactory()
        Task.objects.filter(pk=task.pk).update(status=Task.Status.FAILED, failure_kind=FailureKind.CANCELLED)
        TaskAttemptFactory(task=task, result={"cancel_source": "agent"})
        with (
            patch("teatree.agents.runner_cancel.wait_for_owned_checkout_processes", return_value=(object(),)) as wait,
            patch("teatree.agents.runner_cancel.stop_owned_writers") as stop,
        ):
            settle_cancelled_checkout(task, Path("/checkout"), beat_seconds=0)
        wait.assert_called_once()
        stop.assert_not_called()

    def test_owner_cancel_stops_only_after_two_beat_wait(self) -> None:
        task = TaskFactory()
        Task.objects.filter(pk=task.pk).update(status=Task.Status.FAILED, failure_kind=FailureKind.CANCELLED)
        TaskAttemptFactory(task=task, result={"cancel_source": "owner"})
        with (
            patch("teatree.agents.runner_cancel.wait_for_owned_checkout_processes", return_value=(object(),)) as wait,
            patch("teatree.agents.runner_cancel.stop_owned_writers") as stop,
        ):
            settle_cancelled_checkout(task, Path("/checkout"), beat_seconds=0)
        wait.assert_called_once()
        stop.assert_called_once_with(Path("/checkout"))
