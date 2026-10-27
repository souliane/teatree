"""Finished repair evidence controls task-failure escalation."""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import SelfImproveFiring, Task, TaskAttempt, Ticket, TicketTransition
from teatree.loop.self_improve.budget import BudgetVerdict
from teatree.loop.self_improve.detectors.base import ActionRung
from teatree.loop.self_improve.detectors.lifecycle_incident import LifecycleIncidentDetector
from teatree.loop.self_improve.schedule import Tier, run_tier
from tests.factories import TaskAttemptFactory, TaskFactory


class TaskFailureCoverageTests(TestCase):
    def _ticketed_failed_tasks(self) -> tuple[LifecycleIncidentDetector, Ticket, list[Task], list[TaskAttempt]]:
        tasks = [TaskFactory.create() for _ in range(4)]
        ended_at = timezone.now() - timedelta(hours=1)
        for task in tasks:
            task.fail(reason="ProcessError: worker exited", by_holder=True)
        attempts = [
            TaskAttemptFactory.create(task=task, exit_code=1, error="ProcessError: worker exited", ended_at=ended_at)
            for task in tasks
        ]
        TaskAttempt.objects.filter(pk__in=[attempt.pk for attempt in attempts]).update(
            started_at=ended_at - timedelta(minutes=1)
        )
        detector = LifecycleIncidentDetector(overlay_name="t3-teatree")
        first = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow())
        assert [action.rung for action in first.actions] == [ActionRung.TICKET]
        ticket_id = first.actions[0].firing.ticket_id
        assert ticket_id is not None
        return detector, Ticket.objects.get(pk=ticket_id), tasks, attempts

    @staticmethod
    def _finish_repair_and_shrink(repair: Ticket, tasks: list[Task], *, removed: int = 1) -> None:
        repair.ignore()
        repair.save()
        for task in tasks[:removed]:
            task.ticket.ignore()
            task.ticket.save()

    def test_finished_repair_handles_shrinking_failed_set_without_second_ticket(self) -> None:
        detector, repair, tasks, _attempts = self._ticketed_failed_tasks()
        self._finish_repair_and_shrink(repair, tasks)

        second = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow())
        third = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow())

        failed = [report for report in second.reports if report.payload["kind"] == "task_failed"]
        assert len(failed) == 1
        assert failed[0].payload["count"] == 3
        assert [action.rung for action in second.actions] == [ActionRung.STATUSLINE]
        assert second.actions[0].firing.ticket_id == repair.pk
        assert not third.actions
        assert Ticket.objects.filter(extra__source="self_improve").count() == 1

    def test_finished_repair_demotes_two_survivors_under_sticky_ticket_rung(self) -> None:
        detector, repair, tasks, _attempts = self._ticketed_failed_tasks()
        self._finish_repair_and_shrink(repair, tasks, removed=2)

        second = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow())

        assert [action.rung for action in second.actions] == [ActionRung.STATUSLINE]
        assert second.reports[0].payload["count"] == 2
        assert Ticket.objects.filter(extra__source="self_improve").count() == 1

    def test_attempt_after_repair_finish_opens_new_ticket(self) -> None:
        detector, repair, tasks, attempts = self._ticketed_failed_tasks()
        self._finish_repair_and_shrink(repair, tasks)
        TaskAttempt.objects.filter(pk=attempts[1].pk).update(ended_at=timezone.now() + timedelta(minutes=1))

        second = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow())

        assert [action.rung for action in second.actions] == [ActionRung.TICKET]
        assert second.actions[0].firing.ticket_id != repair.pk
        assert Ticket.objects.filter(extra__source="self_improve").count() == 2

    def test_new_failed_task_after_repair_finish_opens_new_ticket(self) -> None:
        detector, repair, tasks, _attempts = self._ticketed_failed_tasks()
        self._finish_repair_and_shrink(repair, tasks)
        new_task = TaskFactory()
        new_task.fail(reason="ProcessError: worker exited", by_holder=True)
        TaskAttemptFactory(task=new_task, exit_code=1, ended_at=timezone.now() + timedelta(minutes=1))

        second = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow())

        assert [action.rung for action in second.actions] == [ActionRung.TICKET]
        assert second.actions[0].firing.ticket_id != repair.pk

    def test_missing_or_unfinished_attempt_keeps_ticket_rung(self) -> None:
        detector, repair, tasks, attempts = self._ticketed_failed_tasks()
        self._finish_repair_and_shrink(repair, tasks)
        attempts[1].delete()
        assert [report.requested_rung for report in detector.detect() if report.payload["kind"] == "task_failed"] == [
            ActionRung.TICKET
        ]

        TaskAttemptFactory(task=tasks[1], exit_code=1, ended_at=None)
        assert [report.requested_rung for report in detector.detect() if report.payload["kind"] == "task_failed"] == [
            ActionRung.TICKET
        ]

    def test_unfinished_repair_or_resolved_firing_keeps_ticket_rung(self) -> None:
        detector, repair, tasks, _attempts = self._ticketed_failed_tasks()
        tasks[0].ticket.ignore()
        tasks[0].ticket.save()
        assert [report.requested_rung for report in detector.detect() if report.payload["kind"] == "task_failed"] == [
            ActionRung.TICKET
        ]

        repair.ignore()
        repair.save()
        SelfImproveFiring.objects.filter(ticket=repair).update(resolved_at=timezone.now())
        second = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow())

        assert [action.rung for action in second.actions] == [ActionRung.TICKET]
        assert second.actions[0].firing.ticket_id != repair.pk

    def test_finished_ticket_without_transition_keeps_ticket_rung(self) -> None:
        detector, repair, tasks, _attempts = self._ticketed_failed_tasks()
        Ticket.objects.filter(pk=repair.pk).update(state=Ticket.State.IGNORED)
        tasks[0].ticket.ignore()
        tasks[0].ticket.save()

        second = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow())

        assert [action.rung for action in second.actions] == [ActionRung.TICKET]
        assert second.actions[0].firing.ticket_id != repair.pk

    def test_merged_to_retro_uses_first_finish_and_allows_equal_end(self) -> None:
        detector, repair, tasks, attempts = self._ticketed_failed_tasks()
        merged_at = timezone.now() - timedelta(minutes=3)
        TicketTransition.objects.filter(ticket=repair).update(created_at=merged_at - timedelta(minutes=1))
        Ticket.objects.filter(pk=repair.pk).update(state=Ticket.State.RETRO_RECORDED)
        merged = TicketTransition.objects.create(ticket=repair, from_state=repair.state, to_state=Ticket.State.MERGED)
        retro = TicketTransition.objects.create(
            ticket=repair, from_state=Ticket.State.MERGED, to_state=Ticket.State.RETRO_RECORDED
        )
        TicketTransition.objects.filter(pk=merged.pk).update(created_at=merged_at)
        TicketTransition.objects.filter(pk=retro.pk).update(created_at=merged_at + timedelta(minutes=1))
        tasks[0].ticket.ignore()
        tasks[0].ticket.save()
        TaskAttempt.objects.filter(pk=attempts[1].pk).update(ended_at=merged_at)

        covered = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow())
        TaskAttempt.objects.filter(pk=attempts[1].pk).update(ended_at=merged_at + timedelta(seconds=1))
        uncovered = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow())

        assert [action.rung for action in covered.actions] == [ActionRung.STATUSLINE]
        assert [action.rung for action in uncovered.actions] == [ActionRung.TICKET]

    def test_reopened_repair_uses_second_finish(self) -> None:
        detector, repair, tasks, attempts = self._ticketed_failed_tasks()
        first_finish = timezone.now() - timedelta(minutes=3)
        reopened_at = first_finish + timedelta(minutes=1)
        second_finish = first_finish + timedelta(minutes=2)
        TicketTransition.objects.filter(ticket=repair).update(created_at=first_finish - timedelta(minutes=1))
        Ticket.objects.filter(pk=repair.pk).update(state=Ticket.State.IGNORED)
        for from_state, to_state, at in (
            (repair.state, Ticket.State.MERGED, first_finish),
            (Ticket.State.MERGED, Ticket.State.WORK_STARTED, reopened_at),
            (Ticket.State.WORK_STARTED, Ticket.State.IGNORED, second_finish),
        ):
            transition = TicketTransition.objects.create(ticket=repair, from_state=from_state, to_state=to_state)
            TicketTransition.objects.filter(pk=transition.pk).update(created_at=at)
        tasks[0].ticket.ignore()
        tasks[0].ticket.save()
        TaskAttempt.objects.filter(pk=attempts[1].pk).update(ended_at=reopened_at)

        second = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow())

        assert [action.rung for action in second.actions] == [ActionRung.STATUSLINE]
