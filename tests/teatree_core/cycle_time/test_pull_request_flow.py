from datetime import timedelta

import pytest
from django.db.models.signals import post_save
from django.test import TestCase
from django.utils import timezone

from teatree.core.cycle_time.pull_request_flow import merged_pull_request_seconds, open_pull_requests, phase_gap_seconds
from teatree.core.models.pull_request import PullRequest
from teatree.core.models.task import Task
from teatree.core.models.task_attempt import TaskAttempt
from teatree.core.models.ticket import Ticket
from teatree.core.models.transition import TicketTransition
from teatree.core.signals import _auto_enqueue_task
from tests.factories import PullRequestFactory, SessionFactory, TaskFactory, TicketFactory, TicketTransitionFactory


class PullRequestFlowTests(TestCase):
    def setUp(self) -> None:
        self.now = timezone.now()
        post_save.disconnect(_auto_enqueue_task, sender=Task, dispatch_uid="auto_enqueue_task")
        self.addCleanup(post_save.connect, _auto_enqueue_task, sender=Task, dispatch_uid="auto_enqueue_task")

    def _pr(self, *, hours: float, state: str = PullRequest.State.OPEN, overlay: str = "t3-teatree") -> PullRequest:
        ticket = TicketFactory(overlay=overlay)
        task = TaskFactory(ticket=ticket, session=SessionFactory(ticket=ticket), phase="coding")
        Task.objects.filter(pk=task.pk).update(admitted_at=self.now - timedelta(hours=hours))
        row = PullRequestFactory(ticket=ticket, overlay=overlay, state=state)
        return PullRequest.objects.get(pk=row.pk)

    def test_phase_gap_measures_a_pending_next_phase_and_ignores_same_phase_retries(self) -> None:
        ticket = TicketFactory(overlay="t3-teatree")
        session = SessionFactory(ticket=ticket)
        planning = TaskFactory(ticket=ticket, session=session, phase="planning")
        retry = TaskFactory(ticket=ticket, session=session, phase="planning")
        coding = TaskFactory(ticket=ticket, session=session, phase="coding")
        end = self.now - timedelta(hours=30)
        Task.objects.filter(pk=planning.pk).update(created_at=end - timedelta(hours=2), status=Task.Status.COMPLETED)
        Task.objects.filter(pk=retry.pk).update(created_at=end - timedelta(hours=1), status=Task.Status.COMPLETED)
        Task.objects.filter(pk=coding.pk).update(created_at=end + timedelta(minutes=1))
        TaskAttempt.objects.create(task=planning, ended_at=end - timedelta(hours=1), exit_code=0)
        TaskAttempt.objects.create(task=retry, ended_at=end, exit_code=0)

        assert phase_gap_seconds(since=self.now - timedelta(days=2), until=self.now) == pytest.approx((30 * 3600,))

    def test_merge_before_window_is_excluded(self) -> None:
        pr = self._pr(hours=72, state=PullRequest.State.MERGED)
        edge = TicketTransitionFactory(
            ticket=pr.ticket, from_state=Ticket.State.REVIEW_REQUESTED, to_state=Ticket.State.MERGED
        )
        TicketTransition.objects.filter(pk=edge.pk).update(created_at=self.now - timedelta(days=2))

        assert merged_pull_request_seconds(since=self.now - timedelta(days=1), until=self.now) == ()

    def test_merge_after_window_is_excluded(self) -> None:
        pr = self._pr(hours=1, state=PullRequest.State.MERGED)
        edge = TicketTransitionFactory(
            ticket=pr.ticket, from_state=Ticket.State.REVIEW_REQUESTED, to_state=Ticket.State.MERGED
        )
        TicketTransition.objects.filter(pk=edge.pk).update(created_at=self.now + timedelta(days=1))

        assert merged_pull_request_seconds(since=self.now - timedelta(days=1), until=self.now) == ()

    def test_first_admission_sets_open_age_and_merged_duration_after_readmission(self) -> None:
        pr = self._pr(hours=8)
        later = TaskFactory(ticket=pr.ticket, session=SessionFactory(ticket=pr.ticket), phase="coding")
        Task.objects.filter(pk=later.pk).update(admitted_at=self.now - timedelta(hours=2))

        open_row = open_pull_requests(now=self.now)[0]

        assert open_row.age_seconds == pytest.approx(8 * 3600, abs=1)

        PullRequest.objects.filter(pk=pr.pk).update(state=PullRequest.State.MERGED)
        edge = TicketTransitionFactory(
            ticket=pr.ticket, from_state=Ticket.State.REVIEW_REQUESTED, to_state=Ticket.State.MERGED
        )
        TicketTransition.objects.filter(pk=edge.pk).update(created_at=self.now)

        durations = merged_pull_request_seconds(
            since=self.now - timedelta(days=1), until=self.now + timedelta(seconds=1)
        )

        assert durations == pytest.approx((8 * 3600,), abs=1)

    def test_clock_never_starts_after_the_pull_request_was_created(self) -> None:
        pr = self._pr(hours=1)
        PullRequest.objects.filter(pk=pr.pk).update(create_verified_at=self.now - timedelta(hours=8))

        row = open_pull_requests(now=self.now)[0]

        assert row.url == pr.url
        assert row.age_seconds == pytest.approx(8 * 3600, abs=1)

    def test_merged_duration_excludes_other_overlays(self) -> None:
        included = self._pr(hours=6, state=PullRequest.State.MERGED)
        excluded = self._pr(hours=12, state=PullRequest.State.MERGED, overlay="other")
        for pr in (included, excluded):
            edge = TicketTransitionFactory(
                ticket=pr.ticket, from_state=Ticket.State.REVIEW_REQUESTED, to_state=Ticket.State.MERGED
            )
            TicketTransition.objects.filter(pk=edge.pk).update(created_at=self.now)

        durations = merged_pull_request_seconds(
            since=self.now - timedelta(days=1), until=self.now + timedelta(seconds=1), overlay="t3-teatree"
        )

        assert durations == pytest.approx((6 * 3600,), abs=1)

    def test_merge_before_first_admission_is_excluded(self) -> None:
        pr = self._pr(hours=1, state=PullRequest.State.MERGED)
        edge = TicketTransitionFactory(
            ticket=pr.ticket, from_state=Ticket.State.REVIEW_REQUESTED, to_state=Ticket.State.MERGED
        )
        TicketTransition.objects.filter(pk=edge.pk).update(created_at=self.now - timedelta(hours=2))

        durations = merged_pull_request_seconds(since=self.now - timedelta(days=1), until=self.now)

        assert durations == ()

    def test_open_rows_use_first_transition_when_no_task_was_admitted(self) -> None:
        pr = PullRequestFactory(ticket=TicketFactory())
        edge = TicketTransitionFactory(ticket=pr.ticket)
        TicketTransition.objects.filter(pk=edge.pk).update(created_at=self.now - timedelta(hours=20))

        rows = open_pull_requests(now=self.now)

        assert rows[0].url == pr.url
        assert rows[0].age_seconds == pytest.approx(20 * 3600, abs=1)

    def test_open_rows_are_oldest_first_and_exclude_closed_and_other_overlays(self) -> None:
        younger = self._pr(hours=5)
        older = self._pr(hours=15)
        self._pr(hours=30, state=PullRequest.State.CLOSED)
        self._pr(hours=40, overlay="other")

        rows = open_pull_requests(now=self.now, overlay="t3-teatree")

        assert [row.url for row in rows] == [older.url, younger.url]

    def test_open_row_without_a_clock_is_reported_unmeasured(self) -> None:
        pr = PullRequestFactory(ticket=Ticket.objects.create())

        rows = open_pull_requests(now=self.now)

        assert rows[0].url == pr.url
        assert rows[0].age_seconds is None
