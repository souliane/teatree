from datetime import datetime, timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.core.cycle_time import MergeWaitBreakdown, recent_merge_breakdowns
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.pull_request import PullRequest
from teatree.core.models.session import Session
from teatree.core.models.task import Task
from teatree.core.models.task_attempt import TaskAttempt
from teatree.core.models.ticket import Ticket
from teatree.core.models.transition import TicketTransition
from teatree.core.models.usage_window_state import CHECKOUT_PARKED_PREFIX, LIMIT_PARKED_PREFIX


class RecentMergeBreakdownTestCase(TestCase):
    def setUp(self) -> None:
        self.origin = timezone.now() - timedelta(hours=5)
        self.ticket = Ticket.objects.create(state=Ticket.State.MERGED, issue_url="https://example.test/issues/5313")
        PullRequest.objects.create(
            ticket=self.ticket,
            url="https://example.test/pr/5313",
            repo="example/repo",
            iid="5313",
            state=PullRequest.State.MERGED,
        )
        self.transition(Ticket.State.NOT_STARTED, Ticket.State.WORK_STARTED, 0)
        self.transition(Ticket.State.WORK_STARTED, Ticket.State.PLAN_RECORDED, 30)
        self.transition(Ticket.State.PLAN_RECORDED, Ticket.State.CODED, 130)
        self.transition(Ticket.State.CODED, Ticket.State.PR_OPENED, 220)
        self.transition(Ticket.State.PR_OPENED, Ticket.State.MERGED, 230)

    def at(self, minutes: int) -> datetime:
        return self.origin + timedelta(minutes=minutes)

    def transition(self, source: str, target: str, minutes: int) -> None:
        row = TicketTransition.objects.create(ticket=self.ticket, from_state=source, to_state=target)
        TicketTransition.objects.filter(pk=row.pk).update(created_at=self.at(minutes))

    def task(self, phase: str, created: int, admitted: int | None, ended: int | None) -> Task:
        task = Task.objects.create(
            ticket=self.ticket,
            session=Session.objects.create(ticket=self.ticket, overlay="t3-teatree"),
            phase=phase,
            status=Task.Status.COMPLETED,
        )
        Task.objects.filter(pk=task.pk).update(
            created_at=self.at(created),
            admitted_at=self.at(admitted) if admitted is not None else None,
        )
        if ended is not None:
            TaskAttempt.objects.create(task=task, ended_at=self.at(ended), exit_code=0)
        return task

    def park(self, task: Task, minutes: int, prefix: str, holder: str = "", **fields: object) -> TaskAttempt:
        row = TaskAttempt.objects.create(
            task=task,
            ended_at=self.at(minutes),
            error=f"{prefix}refused at {minutes}",
            result={"occupied_by": {"holder": holder}} if holder else {},
            **fields,
        )
        TaskAttempt.objects.filter(pk=row.pk).update(started_at=self.at(minutes))
        return row

    def test_exact_wait_split_and_longest_cause(self) -> None:
        self.task("planning", 0, 0, 30)
        coding = self.task("coding", 30, 100, 130)
        self.park(coding, 40, CHECKOUT_PARKED_PREFIX)
        DeferredQuestion.objects.create(
            question="approval needed",
            parked_task=coding,
            created_at=self.at(130),
            answered_at=self.at(160),
        )
        self.task("shipping", 190, 200, 220)
        TaskAttempt.objects.create(
            task=coding,
            ended_at=self.at(215),
            result={"cancel_source": "agent"},
        )

        row = recent_merge_breakdowns()[0]
        assert isinstance(row, MergeWaitBreakdown)
        assert row.started_at == self.at(0)
        assert row.merged_at == self.at(230)
        assert row.elapsed_seconds == 230 * 60
        assert row.work_seconds == 80 * 60
        assert row.wait_seconds == {
            "not_admitted": 20 * 60,
            "checkout_fence": 60 * 60,
            "sibling_task": 0,
            "quota": 0,
            "human": 30 * 60,
            "no_next_task": 30 * 60,
            "review": 10 * 60,
        }
        assert row.longest_wait is not None
        assert row.longest_wait.cause == "checkout_fence"
        assert row.longest_wait.start == self.at(40)
        assert row.longest_wait.seconds == 60 * 60
        assert row.hand_cancels == 1
        assert row.measured
        assert row.parks_retained

    def test_sibling_hold_and_quota_are_distinct_and_work_wins_overlap(self) -> None:
        sibling = self.task("planning", 0, 0, 50)
        coding = self.task("coding", 10, 90, 130)
        self.park(coding, 20, CHECKOUT_PARKED_PREFIX, holder=f"task:{sibling.pk}")
        self.park(coding, 60, LIMIT_PARKED_PREFIX)

        row = recent_merge_breakdowns()[0]
        assert row.wait_seconds["sibling_task"] == 10 * 60
        assert row.wait_seconds["quota"] == 30 * 60
        assert row.work_seconds == 90 * 60

    def test_sibling_and_fence_parks_do_not_swap_causes(self) -> None:
        sibling = self.task("planning", 0, 0, 40)
        coding = self.task("coding", 10, 100, 130)
        self.park(coding, 20, CHECKOUT_PARKED_PREFIX, holder=f"task:{sibling.pk}")
        self.park(coding, 60, CHECKOUT_PARKED_PREFIX, holder="task:999999")

        row = recent_merge_breakdowns()[0]
        assert row.wait_seconds["sibling_task"] == 20 * 60
        assert row.wait_seconds["checkout_fence"] == 40 * 60

    def test_park_wait_continues_after_its_last_poll_until_next_park(self) -> None:
        task = self.task("planning", 0, 0, 10)
        first = self.park(task, 20, CHECKOUT_PARKED_PREFIX)
        TaskAttempt.objects.filter(pk=first.pk).update(ended_at=self.at(80))
        self.park(task, 100, LIMIT_PARKED_PREFIX)

        row = recent_merge_breakdowns()[0]
        assert row.wait_seconds["checkout_fence"] == 80 * 60
        assert row.wait_seconds["quota"] == 130 * 60

    def test_checkout_attestation_question_is_fence_wait(self) -> None:
        task = self.task("planning", 0, 0, 10)
        self.park(task, 20, CHECKOUT_PARKED_PREFIX)
        DeferredQuestion.objects.create(
            question="accept checkout?",
            dedupe_marker="owner-action:attest:example",
            parked_task=task,
            created_at=self.at(30),
            answered_at=self.at(60),
        )
        row = recent_merge_breakdowns()[0]
        assert row.wait_seconds["human"] == 0
        assert row.wait_seconds["checkout_fence"] == 210 * 60

    def test_checkout_attestation_question_without_park_history_is_fence_wait(self) -> None:
        task = self.task("planning", 0, 0, 10)
        DeferredQuestion.objects.create(
            question="accept checkout?",
            dedupe_marker="owner-action:attest:example",
            parked_task=task,
            created_at=self.at(30),
            answered_at=self.at(60),
        )
        row = recent_merge_breakdowns()[0]
        assert row.wait_seconds["checkout_fence"] == 30 * 60
        assert row.wait_seconds["human"] == 0

    def test_longest_wait_is_one_block_not_a_causes_total(self) -> None:
        self.task("planning", 0, 0, 40)
        first = self.task("coding", 40, 70, 100)
        self.park(first, 50, CHECKOUT_PARKED_PREFIX)
        second = self.task("coding", 100, 130, 160)
        self.park(second, 110, CHECKOUT_PARKED_PREFIX)
        self.task("shipping", 190, 190, 220)

        row = recent_merge_breakdowns()[0]
        assert row.wait_seconds["checkout_fence"] == 40 * 60
        assert row.longest_wait is not None
        assert row.longest_wait.cause == "no_next_task"
        assert row.longest_wait.start == self.at(160)
        assert row.longest_wait.seconds == 30 * 60

    def test_fully_worked_merge_has_no_wait_block(self) -> None:
        self.task("planning", 0, 0, 230)
        row = recent_merge_breakdowns()[0]
        assert row.work_seconds == row.elapsed_seconds
        assert row.longest_wait is None

    def test_no_admission_is_unmeasured_instead_of_all_zero(self) -> None:
        row = recent_merge_breakdowns()[0]
        assert row.measured is False

    def test_no_merge_returns_no_breakdowns(self) -> None:
        TicketTransition.objects.filter(ticket=self.ticket, to_state=Ticket.State.MERGED).delete()
        assert recent_merge_breakdowns() == ()

    def test_old_park_history_is_flagged(self) -> None:
        task = self.task("coding", 0, 0, 30)
        old = timezone.now() - timedelta(days=8)
        Task.objects.filter(pk=task.pk).update(admitted_at=old)
        assert recent_merge_breakdowns()[0].parks_retained is False

    def test_question_linked_by_session_counts_as_human_wait(self) -> None:
        task = self.task("planning", 0, 0, 30)
        DeferredQuestion.objects.create(
            question="approval needed",
            task_session=task.session,
            created_at=self.at(30),
            answered_at=self.at(60),
        )
        assert recent_merge_breakdowns()[0].wait_seconds["human"] == 30 * 60

    def test_post_merge_cancel_does_not_count_for_that_merge(self) -> None:
        task = self.task("planning", 0, 0, 30)
        TaskAttempt.objects.create(task=task, ended_at=self.at(225), result={"cancel_source": "owner"})
        TaskAttempt.objects.create(task=task, ended_at=self.at(231), result={"cancel_source": "owner"})
        assert recent_merge_breakdowns()[0].hand_cancels == 1

    def test_park_between_runs_is_unmeasured_after_readmission(self) -> None:
        task = self.task("planning", 0, 0, 20)
        self.park(task, 20, CHECKOUT_PARKED_PREFIX)
        resumed = TaskAttempt.objects.create(task=task, ended_at=self.at(90), exit_code=0)
        TaskAttempt.objects.filter(pk=resumed.pk).update(started_at=self.at(70))
        Task.objects.filter(pk=task.pk).update(admitted_at=self.at(70))

        row = recent_merge_breakdowns()[0]
        assert row.measured is False

    def test_sibling_work_still_counts_during_another_tasks_terminal_park(self) -> None:
        task = self.task("planning", 0, 0, 20)
        self.park(task, 20, CHECKOUT_PARKED_PREFIX)
        self.task("coding", 10, 10, 50)

        row = recent_merge_breakdowns()[0]
        assert row.work_seconds == 50 * 60
        assert row.wait_seconds["checkout_fence"] == 180 * 60

    def test_park_followed_by_run_without_new_admission_is_unmeasured(self) -> None:
        task = self.task("planning", 0, 0, 20)
        self.park(task, 20, CHECKOUT_PARKED_PREFIX)
        TaskAttempt.objects.create(task=task, ended_at=self.at(90), exit_code=0)
        assert recent_merge_breakdowns()[0].measured is False

    def test_park_without_a_real_attempt_remains_a_wait(self) -> None:
        task = self.task("planning", 0, 0, None)
        Task.objects.filter(pk=task.pk).update(status=Task.Status.PENDING)
        self.park(task, 20, CHECKOUT_PARKED_PREFIX)
        row = recent_merge_breakdowns()[0]
        assert row.measured
        assert row.wait_seconds["checkout_fence"] == 210 * 60

    def test_terminal_coalesced_park_does_not_extend_work(self) -> None:
        task = self.task("planning", 0, 0, 10)
        park = self.park(task, 20, CHECKOUT_PARKED_PREFIX)
        TaskAttempt.objects.filter(pk=park.pk).update(ended_at=self.at(80))
        row = recent_merge_breakdowns()[0]
        assert row.work_seconds == 10 * 60
        assert row.wait_seconds["checkout_fence"] == 210 * 60

    def test_billed_park_ends_real_work(self) -> None:
        task = self.task("planning", 0, 0, None)
        billed = self.park(task, 30, LIMIT_PARKED_PREFIX, input_tokens=100)
        TaskAttempt.objects.filter(pk=billed.pk).update(ended_at=self.at(60))
        row = recent_merge_breakdowns()[0]
        assert row.work_seconds == 60 * 60
        assert row.wait_seconds["quota"] == 170 * 60

    def test_cancel_only_task_ends_work_at_cancel_not_merge(self) -> None:
        task = self.task("planning", 0, 0, None)
        TaskAttempt.objects.create(task=task, ended_at=self.at(20), result={"cancel_source": "agent"})
        row = recent_merge_breakdowns()[0]
        assert row.work_seconds == 20 * 60
        assert row.hand_cancels == 1

    def test_hand_created_tasks_are_inferred_from_first_session_roots(self) -> None:
        planning = self.task("planning", 0, 0, 10)
        first_session = planning.session
        root = Task.objects.create(ticket=self.ticket, session=first_session, phase="coding")
        Task.objects.create(ticket=self.ticket, session=first_session, phase="testing", parent_task=planning)
        Task.objects.create(
            ticket=self.ticket,
            session=Session.objects.create(ticket=self.ticket, overlay="t3-teatree"),
            phase="shipping",
        )
        row = recent_merge_breakdowns()[0]
        assert row.inferred_hand_tasks == 1
        assert root.parent_task_id is None

    def test_cancel_key_not_error_prefix_counts_hand_action(self) -> None:
        task = self.task("planning", 0, 0, 10)
        TaskAttempt.objects.create(task=task, ended_at=self.at(20), error="failed", result={"cancel_source": "agent"})
        assert recent_merge_breakdowns()[0].hand_cancels == 1

    def test_cancelled_prefix_without_key_is_not_hand_action(self) -> None:
        task = self.task("planning", 0, 0, 10)
        TaskAttempt.objects.create(task=task, ended_at=self.at(20), error="cancelled: system")
        assert recent_merge_breakdowns()[0].hand_cancels == 0

    def test_legacy_nonobject_attempt_result_does_not_break_breakdown(self) -> None:
        task = self.task("planning", 0, 0, 30)
        TaskAttempt.objects.create(task=task, ended_at=self.at(35), result=["legacy"])
        TaskAttempt.objects.create(task=task, ended_at=self.at(40), result={"cancel_source": 1})
        assert recent_merge_breakdowns()[0].hand_cancels == 0
