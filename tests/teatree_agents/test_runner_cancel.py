import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.agents.runner_cancel import settle_cancelled_checkout
from teatree.core.modelkit.task_failure_taxonomy import FailureKind
from teatree.core.models import DeferredQuestion, Task
from teatree.core.models.task_claim import claim_generation
from teatree.core.worktree.cancel_processes import CheckoutProcess
from teatree.core.worktree.occupancy import acquire, task_holder_id
from teatree.core.worktree.survivor_notice import apply_stop_answer
from tests.factories import TaskAttemptFactory, TaskFactory, WorktreeFactory


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

    def test_agent_cancel_records_only_descendant_writers_for_a_later_owner_answer(self) -> None:
        task = TaskFactory()
        task.claim(claimed_by="worker", claimed_by_session="session")
        Task.objects.filter(pk=task.pk).update(status=Task.Status.FAILED, failure_kind=FailureKind.CANCELLED)
        attempt = TaskAttemptFactory(task=task, result={"cancel_source": "agent"})
        survivors = (
            CheckoutProcess(pid=101, started=11, descendant=True, marker=False, writer=True, shared_server=False),
            CheckoutProcess(pid=102, started=12, descendant=False, marker=True, writer=True, shared_server=False),
            CheckoutProcess(pid=103, started=13, descendant=True, marker=False, writer=False, shared_server=False),
        )
        with (
            patch("teatree.agents.runner_cancel.wait_for_owned_checkout_processes", return_value=survivors),
            patch("teatree.agents.runner_cancel.current_pid_namespace", return_value="pid:[1]"),
        ):
            settle_cancelled_checkout(task, Path("/checkout"), beat_seconds=0)
        attempt.refresh_from_db()
        assert attempt.result["surviving_writers"] == {
            "namespace": "pid:[1]",
            "claim_generation": claim_generation(task),
            "processes": [{"pid": 101, "start_ticks": 11}],
        }

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

    def test_owner_answer_stops_only_the_captured_writer_for_the_held_checkout(self) -> None:
        task = TaskFactory()
        task.claim(claimed_by="worker", claimed_by_session="session")
        Task.objects.filter(pk=task.pk).update(status=Task.Status.FAILED, failure_kind=FailureKind.CANCELLED)
        checkout = Path(tempfile.mkdtemp())
        self.addCleanup(checkout.rmdir)
        worktree = WorktreeFactory(ticket=task.ticket, extra={"worktree_path": str(checkout)})
        acquire(worktree, holder=task_holder_id(task), holder_session="session")
        TaskAttemptFactory(
            task=task,
            result={
                "cancel_source": "agent",
                "surviving_writers": {
                    "namespace": "pid:[1]",
                    "claim_generation": claim_generation(task),
                    "processes": [{"pid": 101, "start_ticks": 11}],
                },
            },
        )
        question = DeferredQuestion.objects.create(
            question="Should the verified child writers be stopped?",
            dedupe_marker=f"checkout-exit-held:{task.pk}:session",
            answer_text="Stop verified writers",
            answered_at=timezone.now(),
            resolved_via=DeferredQuestion.ResolvedVia.SLACK,
        )
        with (
            patch("teatree.core.worktree.survivor_notice.stop_recorded_writers", return_value=(101,)) as stop,
        ):
            assert apply_stop_answer(question) == (101,)
        stop.assert_called_once_with(checkout, ((101, 11),), "pid:[1]")

        question.resolved_via = DeferredQuestion.ResolvedVia.AGENT
        with patch("teatree.core.worktree.survivor_notice.stop_recorded_writers") as stop:
            assert apply_stop_answer(question) == ()
        stop.assert_not_called()

    def test_owner_answer_does_not_stop_a_writer_captured_by_an_older_claim(self) -> None:
        task = TaskFactory()
        task.claim(claimed_by="worker", claimed_by_session="session")
        Task.objects.filter(pk=task.pk).update(status=Task.Status.FAILED, failure_kind=FailureKind.CANCELLED)
        checkout = Path(tempfile.mkdtemp())
        self.addCleanup(checkout.rmdir)
        worktree = WorktreeFactory(ticket=task.ticket, extra={"worktree_path": str(checkout)})
        acquire(worktree, holder=task_holder_id(task), holder_session="session")
        TaskAttemptFactory(
            task=task,
            result={
                "cancel_source": "agent",
                "surviving_writers": {
                    "namespace": "pid:[1]",
                    "claim_generation": "older claim",
                    "processes": [{"pid": 101, "start_ticks": 11}],
                },
            },
        )
        question = DeferredQuestion.objects.create(
            question="Should the verified child writers be stopped?",
            dedupe_marker=f"checkout-exit-held:{task.pk}:session",
            answer_text="Stop verified writers",
            answered_at=timezone.now(),
            resolved_via=DeferredQuestion.ResolvedVia.SLACK,
        )
        with patch("teatree.core.worktree.survivor_notice.stop_recorded_writers") as stop:
            assert apply_stop_answer(question) == ()
        stop.assert_not_called()
