"""The cycle-time page draws what the measurement layer measured (#3847).

The ask was explicitly graphics, not a CLI table, so these assert the SVG geometry: a
bar's pieces are positioned against the SHARED scale (a fast ticket must render short),
and a trend series is plotted against the shared bucket axis (two edges rarely share
buckets, and packing each series' own points left-to-right would draw them against
different time scales while the page shows one set of labels).
"""

from datetime import timedelta

import pytest
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from teatree.core.models.session import Session
from teatree.core.models.task import Task
from teatree.core.models.task_attempt import TaskAttempt
from teatree.core.models.ticket import Ticket
from teatree.core.models.transition import TicketTransition
from teatree.dash.charts import BAR_WIDTH, TREND_PADDING, TREND_WIDTH, BarInput, line_series, stacked_bar
from teatree.dash.cycle_time import (
    DEFAULT_WINDOW_DAYS,
    MAX_WINDOW_DAYS,
    UNMEASURED_TONE,
    build_cycle_time_view,
    clamp_window_days,
)
from teatree.dash.views.base import NAV_ITEMS

State = Ticket.State
MINUTE = 60.0
_LOOPBACK = {"REMOTE_ADDR": "127.0.0.1"}


def ticket_taking(
    *,
    minutes: float,
    ago_hours: float = 1.0,
    phase_cost: float | None = None,
    admitted: bool = True,
) -> Ticket:
    """A ticket whose `planned -> coded` span lasted *minutes*, finishing *ago_hours* ago."""
    ticket = Ticket.objects.create(state=State.CODED)
    left = timezone.now() - timedelta(hours=ago_hours)
    entered = left - timedelta(minutes=minutes)
    for source, target, stamp in (
        (State.STARTED, State.PLANNED, entered),
        (State.PLANNED, State.CODED, left),
    ):
        row = TicketTransition.objects.create(ticket=ticket, from_state=source, to_state=target)
        TicketTransition.objects.filter(pk=row.pk).update(created_at=stamp)
    task = Task.objects.create(
        ticket=ticket,
        session=Session.objects.create(ticket=ticket, overlay="t3-teatree"),
        phase="coding",
    )
    if admitted:
        Task.objects.filter(pk=task.pk).update(admitted_at=entered + timedelta(minutes=minutes / 4))
    TaskAttempt.objects.create(
        task=task,
        exit_code=0,
        ended_at=left,
        cost_usd=phase_cost,
        cost_is_estimated=True,
    )
    return ticket


class StackedBarGeometryTestCase(TestCase):
    def test_pieces_are_laid_end_to_end_without_a_gap(self) -> None:
        bar = stacked_bar(
            label="#1",
            href="/x/",
            pieces=[
                BarInput(label="wait", tone="building", seconds=25 * MINUTE, muted=True),
                BarInput(label="work", tone="building", seconds=75 * MINUTE),
            ],
            scale_seconds=100 * MINUTE,
        )
        first, second = bar.pieces
        assert first.x == pytest.approx(0.0)
        assert second.x == pytest.approx(first.width)
        assert first.width + second.width == pytest.approx(BAR_WIDTH)

    def test_a_bar_shorter_than_the_scale_renders_shorter(self) -> None:
        """The comparison the chart exists for — a per-bar scale would flatten it away."""
        bar = stacked_bar(
            label="#1",
            href="/x/",
            pieces=[BarInput(label="work", tone="building", seconds=25 * MINUTE)],
            scale_seconds=100 * MINUTE,
        )
        assert bar.pieces[0].width == pytest.approx(BAR_WIDTH / 4)

    def test_the_muted_flag_rides_through_to_the_drawn_piece(self) -> None:
        bar = stacked_bar(
            label="#1",
            href="/x/",
            pieces=[BarInput(label="wait", tone="building", seconds=60.0, muted=True)],
            scale_seconds=60.0,
        )
        assert bar.pieces[0].muted

    def test_an_empty_scale_does_not_divide_by_zero(self) -> None:
        bar = stacked_bar(
            label="#1",
            href="/x/",
            pieces=[BarInput(label="work", tone="building", seconds=0.0)],
            scale_seconds=0.0,
        )
        assert bar.pieces[0].width == pytest.approx(0.0)


class TrendGeometryTestCase(TestCase):
    AXIS = ("Aug 01", "Aug 02", "Aug 03")

    def test_a_series_is_positioned_by_its_bucket_slot_not_its_own_index(self) -> None:
        series = line_series(
            label="planned → coded",
            tone="building",
            points=[("Aug 03", 60.0)],
            axis=self.AXIS,
            scale_seconds=60.0,
        )
        assert series.points[0].x == pytest.approx(TREND_WIDTH - TREND_PADDING)

    def test_a_bucket_outside_the_axis_is_dropped_rather_than_plotted_at_zero(self) -> None:
        series = line_series(
            label="planned → coded",
            tone="building",
            points=[("Jul 01", 60.0), ("Aug 02", 30.0)],
            axis=self.AXIS,
            scale_seconds=60.0,
        )
        assert [point.label for point in series.points] == ["Aug 02"]

    def test_the_peak_sample_sits_at_the_top_of_the_plot(self) -> None:
        series = line_series(
            label="planned → coded",
            tone="building",
            points=[("Aug 01", 120.0)],
            axis=self.AXIS,
            scale_seconds=120.0,
        )
        assert series.points[0].y == pytest.approx(TREND_PADDING)

    def test_the_polyline_is_the_points_in_order(self) -> None:
        series = line_series(
            label="planned → coded",
            tone="building",
            points=[("Aug 01", 60.0), ("Aug 02", 30.0)],
            axis=self.AXIS,
            scale_seconds=60.0,
        )
        assert series.polyline.count(",") == 2


class CycleTimeViewTestCase(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        for minutes in (20, 40, 90):
            ticket_taking(minutes=minutes, phase_cost=0.5)

    def test_the_aggregate_leads_with_the_slowest_edge(self) -> None:
        view = build_cycle_time_view(window_days=7)
        assert (view.edges[0].from_state, view.edges[0].to_state) == (State.PLANNED, State.CODED)
        assert view.edges[0].samples == 3

    def test_each_edge_carries_a_median_and_a_p90(self) -> None:
        edge = build_cycle_time_view(window_days=7).edges[0]
        assert edge.median == "40m"
        assert edge.p90 == "1h 20m"

    def test_every_recent_ticket_gets_a_bar(self) -> None:
        assert len(build_cycle_time_view(window_days=7).tickets) == 3

    def test_each_measured_phase_contributes_a_waiting_and_a_working_piece(self) -> None:
        row = build_cycle_time_view(window_days=7).tickets[0]
        assert [(piece.label, piece.muted) for piece in row.bar.pieces] == [
            ("coding · waiting", True),
            ("coding · working", False),
        ]

    def test_the_bars_share_one_scale_so_the_longest_fills_the_axis(self) -> None:
        rows = build_cycle_time_view(window_days=7).tickets
        widest = max(sum(piece.width for piece in row.bar.pieces) for row in rows)
        assert widest == pytest.approx(BAR_WIDTH)

    def test_cost_rides_alongside_the_time_and_is_marked_as_an_estimate(self) -> None:
        row = build_cycle_time_view(window_days=7).tickets[0]
        assert row.cost_usd == pytest.approx(0.5)
        assert row.cost_is_wholly_estimated

    def test_an_unmeasurable_split_is_one_neutral_piece_rather_than_all_waiting(self) -> None:
        """A pre-admission-stamp ticket must not render as a bar that is entirely queue wait."""
        Ticket.objects.all().delete()
        ticket_taking(minutes=60, admitted=False)
        row = build_cycle_time_view(window_days=7).tickets[0]
        assert row.work_measured is False
        assert [(piece.label, piece.tone) for piece in row.bar.pieces] == [
            ("coding · split unmeasured", UNMEASURED_TONE),
        ]

    def test_a_window_with_no_activity_renders_empty_rather_than_raising(self) -> None:
        TicketTransition.objects.all().delete()
        view = build_cycle_time_view(window_days=7)
        assert view.edges == ()
        assert view.tickets == ()
        assert view.trend == ()


class WindowClampTestCase(TestCase):
    def test_an_unreadable_window_falls_back_to_the_default(self) -> None:
        assert clamp_window_days("not-a-number") == DEFAULT_WINDOW_DAYS

    def test_an_oversized_window_is_capped(self) -> None:
        assert clamp_window_days("9000") == MAX_WINDOW_DAYS

    def test_a_zero_window_is_raised_to_one_day(self) -> None:
        assert clamp_window_days("0") == 1


class CycleTimePageTestCase(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = ticket_taking(minutes=85, phase_cost=1.5)

    def test_the_page_is_in_the_nav(self) -> None:
        assert ("dash:cycle_time", "Cycle time") in NAV_ITEMS

    def test_the_page_renders_the_transition_the_data_contains(self) -> None:
        response = self.client.get(reverse("dash:cycle_time"), **_LOOPBACK)
        assert response.status_code == 200
        assert f"{State.PLANNED} → {State.CODED}" in response.content.decode()

    def test_the_page_draws_charts_rather_than_only_tabulating(self) -> None:
        body = self.client.get(reverse("dash:cycle_time"), **_LOOPBACK).content.decode()
        assert 'class="chart-bar"' in body
        assert "<rect" in body

    def test_the_requested_window_is_honoured(self) -> None:
        response = self.client.get(reverse("dash:cycle_time"), {"days": "3"}, **_LOOPBACK)
        assert response.context["view"].window_days == 3

    def test_an_unmeasured_split_is_labelled_rather_than_shown_as_zero(self) -> None:
        Ticket.objects.all().delete()
        ticket_taking(minutes=60, admitted=False)
        body = self.client.get(reverse("dash:cycle_time"), **_LOOPBACK).content.decode()
        assert "unmeasured" in body

    def test_an_out_of_range_window_does_not_500(self) -> None:
        response = self.client.get(reverse("dash:cycle_time"), {"days": "-4"}, **_LOOPBACK)
        assert response.status_code == 200
        assert response.context["view"].window_days == 1
