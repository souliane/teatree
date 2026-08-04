"""The per-ticket timeline is computed from real stamps, never from ``TaskAttempt.started_at``.

``TaskAttempt.started_at`` (``auto_now_add``) and ``ended_at`` are BOTH written when
the row is inserted at agent completion, so their difference is ~0 for every attempt
the factory has ever recorded. A work-time built on it reports "no agent ever worked"
while the phase visibly took an hour. The stamps that mean what they say are
``Task.admitted_at`` (the runner handoff) and ``TaskAttempt.ended_at``, and the phase
boundary is a ``TicketTransition`` row — this module pins all three.
"""

from datetime import datetime, timedelta

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.cycle_time import PhaseSegment, build_ticket_timeline
from teatree.core.models.session import Session
from teatree.core.models.task import Task
from teatree.core.models.task_attempt import TaskAttempt
from teatree.core.models.ticket import Ticket
from teatree.core.models.transition import TicketTransition

State = Ticket.State

MINUTE = 60.0
ORIGIN = datetime(2026, 8, 1, 9, 0, tzinfo=timezone.get_current_timezone())


def at(minutes: float) -> datetime:
    return ORIGIN + timedelta(minutes=minutes)


def record_transition(ticket: Ticket, *, source: str, target: str, minutes: float) -> TicketTransition:
    """A transition stamped at *minutes* past :data:`ORIGIN` (``created_at`` is ``auto_now_add``)."""
    row = TicketTransition.objects.create(ticket=ticket, from_state=source, to_state=target, triggered_by="test")
    TicketTransition.objects.filter(pk=row.pk).update(created_at=at(minutes))
    row.refresh_from_db()
    return row


def record_task(ticket: Ticket, *, phase: str, queued: float, admitted: float | None) -> Task:
    task = Task.objects.create(
        ticket=ticket,
        session=Session.objects.create(ticket=ticket, overlay="t3-teatree"),
        phase=phase,
        execution_target=Task.ExecutionTarget.INTERACTIVE,
    )
    Task.objects.filter(pk=task.pk).update(
        created_at=at(queued),
        admitted_at=None if admitted is None else at(admitted),
    )
    task.refresh_from_db()
    return task


def record_attempt(task: Task, *, ended: float, row_written: float | None = None, **fields: object) -> TaskAttempt:
    """An attempt whose ``started_at`` mirrors production: stamped when the row is written.

    *row_written* defaults to *ended* — the real shape, where insert and completion are
    the same instant. A test that moves it proves the reader never looks at it.
    """
    attempt = TaskAttempt.objects.create(
        task=task,
        execution_target=Task.ExecutionTarget.HEADLESS,
        exit_code=0,
        ended_at=at(ended),
        **fields,
    )
    TaskAttempt.objects.filter(pk=attempt.pk).update(started_at=at(ended if row_written is None else row_written))
    attempt.refresh_from_db()
    return attempt


class PhaseDurationsComeFromTransitionDeltasTestCase(TestCase):
    """A constructed transition sequence, and the durations it must produce."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create(state=State.TESTED)
        record_transition(cls.ticket, source=State.NOT_STARTED, target=State.STARTED, minutes=0)
        record_transition(cls.ticket, source=State.STARTED, target=State.PLANNED, minutes=5)
        record_transition(cls.ticket, source=State.PLANNED, target=State.CODED, minutes=90)
        record_transition(cls.ticket, source=State.CODED, target=State.TESTED, minutes=110)

    def test_each_segment_measures_the_time_spent_in_its_from_state(self) -> None:
        timeline = build_ticket_timeline(self.ticket.pk)
        measured = [(segment.from_state, segment.to_state, segment.seconds) for segment in timeline.segments]
        assert measured == [
            (State.STARTED, State.PLANNED, 5 * MINUTE),
            (State.PLANNED, State.CODED, 85 * MINUTE),
            (State.CODED, State.TESTED, 20 * MINUTE),
        ]

    def test_the_first_transition_opens_the_timeline_rather_than_measuring_a_span(self) -> None:
        """``not_started -> started`` has no predecessor, so no elapsed time precedes it."""
        timeline = build_ticket_timeline(self.ticket.pk)
        assert timeline.started_at == at(0)
        assert State.NOT_STARTED not in [segment.from_state for segment in timeline.segments]

    def test_lead_time_spans_the_first_transition_to_the_last(self) -> None:
        assert build_ticket_timeline(self.ticket.pk).lead_time_seconds == 110 * MINUTE

    def test_lead_time_equals_the_sum_of_the_segments(self) -> None:
        timeline = build_ticket_timeline(self.ticket.pk)
        assert sum(segment.seconds for segment in timeline.segments) == timeline.lead_time_seconds

    def test_each_segment_names_the_phase_that_produces_its_target_state(self) -> None:
        by_edge = {(s.from_state, s.to_state): s.phase for s in build_ticket_timeline(self.ticket.pk).segments}
        assert by_edge[State.PLANNED, State.CODED] == "coding"
        assert by_edge[State.CODED, State.TESTED] == "testing"

    def test_a_ticket_with_no_transitions_has_an_empty_timeline_rather_than_raising(self) -> None:
        timeline = build_ticket_timeline(Ticket.objects.create(state=State.NOT_STARTED).pk)
        assert timeline.segments == ()
        assert timeline.lead_time_seconds == pytest.approx(0.0)


class WorkTimeIsDistinguishedFromQueueWaitTestCase(TestCase):
    """The 85-minute coding phase, split into the 60 an agent held it and the 25 it waited."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create(state=State.CODED)
        record_transition(cls.ticket, source=State.NOT_STARTED, target=State.STARTED, minutes=0)
        record_transition(cls.ticket, source=State.STARTED, target=State.PLANNED, minutes=5)
        record_transition(cls.ticket, source=State.PLANNED, target=State.CODED, minutes=90)
        cls.task = record_task(cls.ticket, phase="coding", queued=5, admitted=25)
        cls.attempt = record_attempt(cls.task, ended=85)

    def _coding(self) -> PhaseSegment:
        segments = build_ticket_timeline(self.ticket.pk).segments
        return next(s for s in segments if s.to_state == State.CODED)

    def test_work_time_spans_the_admission_stamp_to_the_attempt_end(self) -> None:
        assert self._coding().work_seconds == 60 * MINUTE

    def test_work_time_is_not_the_attempt_rows_own_started_to_ended_difference(self) -> None:
        """Both stamps are written at completion, so their difference is 0 — the trap."""
        row = TaskAttempt.objects.get(pk=self.attempt.pk)
        assert (row.ended_at - row.started_at).total_seconds() == pytest.approx(0.0)
        assert self._coding().work_seconds > 0.0

    def test_moving_the_attempts_started_at_does_not_move_the_computed_work_time(self) -> None:
        before = self._coding().work_seconds
        TaskAttempt.objects.filter(pk=self.attempt.pk).update(started_at=at(-500))
        assert self._coding().work_seconds == before

    def test_queue_wait_is_the_elapsed_time_no_agent_held_the_phase(self) -> None:
        assert self._coding().queue_seconds == 25 * MINUTE

    def test_the_split_accounts_for_the_whole_segment(self) -> None:
        coding = self._coding()
        assert coding.queue_seconds + coding.work_seconds == coding.seconds

    def test_a_phase_no_agent_was_ever_admitted_to_is_all_queue_wait(self) -> None:
        segments = build_ticket_timeline(self.ticket.pk).segments
        planning = next(s for s in segments if s.to_state == State.PLANNED)
        assert planning.work_seconds == pytest.approx(0.0)
        assert planning.queue_seconds == planning.seconds

    def test_the_ticket_totals_the_split_across_every_segment(self) -> None:
        timeline = build_ticket_timeline(self.ticket.pk)
        assert timeline.lead_time_seconds == 90 * MINUTE
        assert timeline.work_seconds == 60 * MINUTE
        assert timeline.queue_seconds == 30 * MINUTE


class OverlappingAndUnfinishedAttemptsTestCase(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create(state=State.CODED)
        record_transition(cls.ticket, source=State.NOT_STARTED, target=State.PLANNED, minutes=0)
        record_transition(cls.ticket, source=State.PLANNED, target=State.CODED, minutes=100)

    def _coding_work(self) -> float:
        segments = build_ticket_timeline(self.ticket.pk).segments
        return next(s for s in segments if s.to_state == State.CODED).work_seconds

    def test_two_agents_holding_the_phase_at_once_are_counted_as_one_elapsed_stretch(self) -> None:
        record_attempt(record_task(self.ticket, phase="coding", queued=0, admitted=10), ended=50)
        record_attempt(record_task(self.ticket, phase="coding", queued=0, admitted=30), ended=70)
        assert self._coding_work() == 60 * MINUTE

    def test_an_admitted_task_that_never_recorded_an_attempt_is_working_until_the_phase_ends(self) -> None:
        record_task(self.ticket, phase="coding", queued=0, admitted=40)
        assert self._coding_work() == 60 * MINUTE

    def test_a_never_admitted_task_contributes_no_work_time(self) -> None:
        record_attempt(record_task(self.ticket, phase="coding", queued=0, admitted=None), ended=50)
        assert self._coding_work() == pytest.approx(0.0)

    def test_work_recorded_outside_the_segment_is_clipped_to_it(self) -> None:
        record_attempt(record_task(self.ticket, phase="coding", queued=0, admitted=-30), ended=140)
        assert self._coding_work() == 100 * MINUTE

    def test_another_phases_task_does_not_count_as_this_phases_work(self) -> None:
        record_attempt(record_task(self.ticket, phase="reviewing", queued=0, admitted=10), ended=50)
        assert self._coding_work() == pytest.approx(0.0)


class CostRidesAlongsideTimeTestCase(TestCase):
    """#3673's provenance columns, summed per phase — never a bare number."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create(state=State.CODED)
        record_transition(cls.ticket, source=State.NOT_STARTED, target=State.PLANNED, minutes=0)
        record_transition(cls.ticket, source=State.PLANNED, target=State.CODED, minutes=100)
        task = record_task(cls.ticket, phase="coding", queued=0, admitted=10)
        record_attempt(task, ended=50, cost_usd=1.25, cost_is_estimated=False)
        record_attempt(task, ended=60, cost_usd=0.75, cost_is_estimated=True)

    def _coding(self) -> PhaseSegment:
        segments = build_ticket_timeline(self.ticket.pk).segments
        return next(s for s in segments if s.to_state == State.CODED)

    def test_the_phase_carries_its_total_spend(self) -> None:
        assert self._coding().cost_usd == pytest.approx(2.0)

    def test_the_estimated_share_is_reported_separately_from_the_total(self) -> None:
        assert self._coding().cost_estimated_usd == pytest.approx(0.75)

    def test_the_attempt_count_backs_the_spend_figure(self) -> None:
        assert self._coding().attempts == 2

    def test_the_ticket_totals_spend_across_its_phases(self) -> None:
        timeline = build_ticket_timeline(self.ticket.pk)
        assert timeline.cost_usd == pytest.approx(2.0)
        assert timeline.cost_estimated_usd == pytest.approx(0.75)
