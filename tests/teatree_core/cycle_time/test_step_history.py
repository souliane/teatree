from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.core import cycle_time
from teatree.core.cycle_time.step_history import StepHistory
from teatree.core.models import Session, Task, TaskAttempt, Ticket
from teatree.core.models.merge_clear import MergeAudit, MergeClear
from teatree.core.models.transition import TicketTransition
from teatree.core.models.usage_window_state import LIMIT_PARKED_PREFIX


def _run(*, minutes: int, ended_hours_ago: int = 0, error: str = "", status: str = Task.Status.COMPLETED) -> None:
    ticket = Ticket.objects.create(
        issue_url=f"https://github.com/acme/widgets/issues/{minutes + 1000 + ended_hours_ago}"
    )
    task = Task.objects.create(
        ticket=ticket, session=Session.objects.create(ticket=ticket), phase="code", status=status
    )
    ended = timezone.now() - timedelta(hours=ended_hours_ago)
    Task.objects.filter(pk=task.pk).update(admitted_at=ended - timedelta(minutes=minutes))
    TaskAttempt.objects.create(task=task, ended_at=ended, exit_code=1 if error else 0, error=error)


class TestStepHistory(TestCase):
    def test_step_history_is_not_reexported_by_the_package(self) -> None:
        assert "StepHistory" not in cycle_time.__all__
        assert "StepEstimate" not in cycle_time.__all__
        assert not hasattr(cycle_time, "StepHistory")
        assert not hasattr(cycle_time, "StepEstimate")

    def test_history_uses_last_real_completed_run_and_widens_window(self) -> None:
        for minutes in (10, 20, 30, 40):
            _run(minutes=minutes)
        _run(minutes=50, ended_hours_ago=48)
        _run(minutes=100, error="cancelled: operator")
        _run(minutes=120, error=f"{LIMIT_PARKED_PREFIX}retry later")
        _run(minutes=200, status=Task.Status.FAILED)
        estimate = StepHistory.measure(timezone.now()).estimate("coding")
        assert estimate is not None
        assert (estimate.median_minutes, estimate.p80_minutes, estimate.n, estimate.window) == (30, 42, 5, "7d")

    def test_missing_step_uses_named_slowest_measured_default(self) -> None:
        for minutes in (10, 20, 30, 40, 50):
            _run(minutes=minutes)
        history = StepHistory.measure(timezone.now())
        estimate = history.estimate("merge")
        assert estimate is not None
        assert estimate.basis == "default: slowest measured step (coding)"
        assert estimate.median_minutes == 30
        assert estimate.p80_minutes == 42
        assert estimate.window == "24h"
        assert history.remaining_default(5) == (25, 37, "default: slowest surviving step (coding)")
        assert history.remaining_default(50) is None

    def test_sparse_default_uses_slowest_median_and_p80_independently(self) -> None:
        for phase, durations in (("coding", (30, 30, 30, 30, 30)), ("reviewing", (10, 10, 10, 10, 200))):
            for index, duration in enumerate(durations):
                ticket = Ticket.objects.create(issue_url=f"https://github.com/acme/widgets/issues/{phase}-{index}")
                task = Task.objects.create(
                    ticket=ticket,
                    session=Session.objects.create(ticket=ticket),
                    phase=phase,
                    status=Task.Status.COMPLETED,
                )
                ended = timezone.now() - timedelta(minutes=1)
                Task.objects.filter(pk=task.pk).update(admitted_at=ended - timedelta(minutes=duration))
                TaskAttempt.objects.create(task=task, ended_at=ended, exit_code=0)
        estimate = StepHistory.measure(timezone.now()).estimate("merge")
        assert estimate is not None
        assert estimate.median_minutes == 30
        assert estimate.p80_minutes == 48
        assert "coding" in estimate.basis
        assert "reviewing" in estimate.basis

    def test_readmission_after_the_last_run_is_not_a_duration(self) -> None:
        _run(minutes=-5)
        assert StepHistory.measure(timezone.now()).spans == {}

    def test_empty_history_has_no_default(self) -> None:
        assert StepHistory.measure(timezone.now()).estimate("coding") is None

    def test_thin_history_alone_is_not_a_numeric_default(self) -> None:
        _run(minutes=60)
        assert StepHistory.measure(timezone.now()).estimate("coding") is None

    def test_running_step_widens_to_seven_days_when_recent_samples_all_finished(self) -> None:
        now = timezone.now()
        history = StepHistory(
            now=now,
            spans={
                "reviewing": [
                    *((now - timedelta(hours=1), 30.0) for _ in range(5)),
                    *((now - timedelta(days=2), 60.0) for _ in range(5)),
                ]
            },
        )
        assert history.remaining_measured("reviewing", 45) == (15.0, 15.0, "7d")

    def test_completed_task_uses_last_non_park_attempt_even_if_failed(self) -> None:
        for minutes in (50, 55, 60, 65, 70):
            _run(minutes=minutes, error="temporary failure" if minutes == 60 else "")
        estimate = StepHistory.measure(timezone.now()).estimate("coding")
        assert estimate is not None
        assert estimate.median_minutes == 60

    def test_billed_park_is_a_real_attempt_end(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://github.com/acme/widgets/issues/777")
        task = Task.objects.create(
            ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding", status=Task.Status.COMPLETED
        )
        ended = timezone.now() - timedelta(minutes=1)
        Task.objects.filter(pk=task.pk).update(admitted_at=ended - timedelta(minutes=40))
        TaskAttempt.objects.create(
            task=task, ended_at=ended, exit_code=1, error=f"{LIMIT_PARKED_PREFIX}retry later", input_tokens=10
        )
        history = StepHistory.measure(timezone.now())
        assert history.spans["coding"] == [(ended, 40)]

    def test_deploy_pairs_with_first_delivery_after_each_merge(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://github.com/acme/widgets/issues/888")
        merged = timezone.now() - timedelta(hours=2)
        clear = MergeClear.objects.create(
            ticket=ticket,
            pr_id=888,
            slug="acme/widgets",
            reviewed_sha="a" * 40,
            reviewer_identity="reviewer",
            gh_verify_result=MergeClear.VerifyResult.GREEN,
            blast_class=MergeClear.BlastClass.LOGIC,
            issued_at=merged - timedelta(minutes=5),
        )
        MergeAudit.objects.create(clear=clear, merged_sha="a" * 40, required_checks_status="success", merged_at=merged)
        for stamp in (merged - timedelta(hours=1), merged + timedelta(minutes=15)):
            transition = TicketTransition.objects.create(
                ticket=ticket, from_state=Ticket.State.MERGED, to_state=Ticket.State.DELIVERED
            )
            TicketTransition.objects.filter(pk=transition.pk).update(created_at=stamp)
        history = StepHistory.measure(timezone.now())
        assert history.spans["deploy"] == [(merged + timedelta(minutes=15), 15)]
