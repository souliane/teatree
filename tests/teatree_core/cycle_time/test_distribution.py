"""The aggregate view reproduces #3994's table shape — median AND p90 per transition.

#3994 produced that table once, by hand, over a 16-hour window, and it named the two
whales that were 71% of the cycle. A median alone hides the tail, so each edge carries
its p90 too: the tail is what blocks a delivery day.
"""

from datetime import timedelta

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.cycle_time import transition_distribution, transition_trend
from teatree.core.models.ticket import Ticket
from tests.teatree_core.cycle_time.test_timeline import MINUTE, ORIGIN, at, record_transition

State = Ticket.State


def ticket_with_edge(*, source: str, target: str, start: float, minutes: float) -> Ticket:
    """A ticket whose only measurable span is *source* -> *target*, lasting *minutes*."""
    ticket = Ticket.objects.create(state=target)
    record_transition(ticket, source=State.NOT_STARTED, target=source, minutes=start)
    record_transition(ticket, source=source, target=target, minutes=start + minutes)
    return ticket


class TransitionDistributionTestCase(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        for minutes in (10, 20, 30, 40, 100):
            ticket_with_edge(source=State.PLANNED, target=State.CODED, start=0, minutes=minutes)
        for minutes in (2, 4):
            ticket_with_edge(source=State.CODED, target=State.TESTED, start=0, minutes=minutes)

    def _stats(self) -> dict[tuple[str, str], object]:
        window = transition_distribution(since=ORIGIN - timedelta(days=1))
        return {(stat.from_state, stat.to_state): stat for stat in window}

    def test_each_edge_reports_how_many_samples_back_it(self) -> None:
        assert self._stats()[State.PLANNED, State.CODED].samples == 5

    def test_the_median_is_the_middle_sample(self) -> None:
        assert self._stats()[State.PLANNED, State.CODED].median_seconds == 30 * MINUTE

    def test_the_p90_exposes_the_tail_the_median_hides(self) -> None:
        assert self._stats()[State.PLANNED, State.CODED].p90_seconds == 76 * MINUTE

    def test_a_two_sample_edge_still_reports_both_figures(self) -> None:
        stat = self._stats()[State.CODED, State.TESTED]
        assert stat.median_seconds == 3 * MINUTE
        assert stat.p90_seconds == pytest.approx(3.8 * MINUTE)

    def test_the_table_leads_with_the_slowest_edge(self) -> None:
        """#3994's shape: the whale is the first row, so nobody has to think to look."""
        rows = transition_distribution(since=ORIGIN - timedelta(days=1))
        assert [(row.from_state, row.to_state) for row in rows] == [
            (State.PLANNED, State.CODED),
            (State.CODED, State.TESTED),
        ]

    def test_a_window_that_predates_every_sample_is_empty_rather_than_raising(self) -> None:
        assert transition_distribution(since=timezone.now() + timedelta(days=1)) == ()


class WindowBoundsTestCase(TestCase):
    def test_a_span_that_ended_before_the_window_is_excluded(self) -> None:
        ticket_with_edge(source=State.PLANNED, target=State.CODED, start=0, minutes=10)
        assert transition_distribution(since=at(60)) == ()

    def test_a_span_that_ended_inside_the_window_is_included_even_when_it_started_before(self) -> None:
        """The whale spans are long — dropping one for starting early is how a tail vanishes."""
        ticket_with_edge(source=State.PLANNED, target=State.CODED, start=0, minutes=120)
        stats = transition_distribution(since=at(60))
        assert [stat.median_seconds for stat in stats] == [120 * MINUTE]


class TransitionTrendTestCase(TestCase):
    """Is the factory getting slower — the same edge, bucketed over time."""

    @classmethod
    def setUpTestData(cls) -> None:
        day = 24 * 60
        for minutes in (10, 20):
            ticket_with_edge(source=State.PLANNED, target=State.CODED, start=0, minutes=minutes)
        for minutes in (60, 80):
            ticket_with_edge(source=State.PLANNED, target=State.CODED, start=day, minutes=minutes)

    def test_the_edge_carries_one_point_per_bucket_in_chronological_order(self) -> None:
        series = transition_trend(since=ORIGIN - timedelta(days=1), bucket=timedelta(days=1))
        coding = next(row for row in series if (row.from_state, row.to_state) == (State.PLANNED, State.CODED))
        assert [point.median_seconds for point in coding.points] == [15 * MINUTE, 70 * MINUTE]
        assert [point.bucket_start for point in coding.points] == sorted(p.bucket_start for p in coding.points)

    def test_a_bucket_reports_the_sample_count_behind_its_point(self) -> None:
        series = transition_trend(since=ORIGIN - timedelta(days=1), bucket=timedelta(days=1))
        coding = next(row for row in series if (row.from_state, row.to_state) == (State.PLANNED, State.CODED))
        assert [point.samples for point in coding.points] == [2, 2]
