"""The cycle-time page draws what the measurement layer measured (#3847).

The ask was explicitly graphics, not a CLI table, so these assert the SVG geometry: a
bar's pieces are positioned against the SHARED scale (a fast ticket must render short),
and a trend series is plotted against the shared bucket axis (two edges rarely share
buckets, and packing each series' own points left-to-right would draw them against
different time scales while the page shows one set of labels).
"""

import re
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.db.models.signals import post_save
from django.urls import reverse
from django.utils import timezone

from teatree.core.cycle_time import build_merge_wait_breakdowns
from teatree.core.models.pull_request import PullRequest
from teatree.core.models.session import Session
from teatree.core.models.task import Task
from teatree.core.models.task_attempt import TaskAttempt
from teatree.core.models.ticket import Ticket
from teatree.core.models.transition import TicketTransition
from teatree.core.signals import _auto_enqueue_task
from teatree.dash.charts import BAR_WIDTH, TREND_PADDING, TREND_WIDTH, BarInput, line_series, stacked_bar
from teatree.dash.cycle_time import (
    DEFAULT_WINDOW_DAYS,
    MAX_WINDOW_DAYS,
    UNMEASURED_TONE,
    build_cycle_time_view,
    build_merge_summary,
    clamp_window_days,
)
from teatree.dash.views.base import NAV_ITEMS
from tests.factories import PullRequestFactory
from tests.teatree_dash.staff_testcase import StaffDashboardTestCase as TestCase

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
        (State.WORK_STARTED, State.PLAN_RECORDED, entered),
        (State.PLAN_RECORDED, State.CODED, left),
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


def started_pull_request(*, hours_ago: float, state: str = PullRequest.State.OPEN) -> PullRequest:
    ticket = Ticket.objects.create()
    task = Task.objects.create(
        ticket=ticket,
        session=Session.objects.create(ticket=ticket, overlay="t3-teatree"),
        phase="coding",
    )
    Task.objects.filter(pk=task.pk).update(admitted_at=timezone.now() - timedelta(hours=hours_ago))
    return PullRequestFactory.create(ticket=ticket, state=state)


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
        assert (view.edges[0].from_state, view.edges[0].to_state) == (State.PLAN_RECORDED, State.CODED)
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


class MergeWaitDisplayTestCase(TestCase):
    def setUp(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://example.test/issues/5313", state=State.MERGED)
        PullRequest.objects.create(
            ticket=ticket,
            url="https://example.test/pull/5313",
            repo="example/repo",
            iid="5313",
            state=PullRequest.State.MERGED,
        )
        start = timezone.now() - timedelta(hours=2)
        for source, target, stamp in (
            (State.NOT_STARTED, State.WORK_STARTED, start),
            (State.WORK_STARTED, State.PR_OPENED, start + timedelta(hours=1)),
            (State.PR_OPENED, State.MERGED, start + timedelta(minutes=90)),
        ):
            row = TicketTransition.objects.create(ticket=ticket, from_state=source, to_state=target)
            TicketTransition.objects.filter(pk=row.pk).update(created_at=stamp)
        task = Task.objects.create(
            ticket=ticket,
            session=Session.objects.create(ticket=ticket, overlay="t3-teatree"),
            phase="coding",
            status=Task.Status.COMPLETED,
        )
        Task.objects.filter(pk=task.pk).update(created_at=start, admitted_at=start)
        TaskAttempt.objects.create(task=task, ended_at=start + timedelta(hours=1))

    def test_cycle_time_lists_the_last_merges_and_all_wait_causes(self) -> None:
        response = self.client.get(reverse("dash:cycle_time"), **_LOOPBACK)
        assert response.status_code == 200
        content = response.content.decode()
        assert "Last 10 merged pull requests" in content
        section = content.split('id="merge-waits"', 1)[1].split("</section>", 1)[0]
        headers = [
            re.sub(r"<[^>]*>", "", header).strip() for header in re.findall(r"<th[^>]*>(.*?)</th>", section, re.DOTALL)
        ]
        assert headers == [
            "ticket",
            "elapsed",
            "working",
            "Waiting for a free seat",
            "Not admitted, cause not recorded",
            "Checkout fence",
            "Sibling task holds checkout",
            "Quota lane",
            "Waiting for a human",
            "No next task",
            "Review",
            "longest wait",
            "Hand cancels",
            "Tasks created by hand (inferred)",
        ]
        row = re.findall(r"<tr>(.*?)</tr>", section, re.DOTALL)[1]
        cells = [re.sub(r"<[^>]*>", "", cell).strip() for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)]
        assert 'href="https://example.test/pull/5313"' in row
        assert cells[:11] == ["#5313", "1h 30m", "1h", "0s", "0s", "0s", "0s", "0s", "0s", "0s", "30m"]
        assert cells[11].startswith("Review (30m from")
        assert cells[12:] == ["0", "1"]
        assert "may include false positives" in content
        assert "not recorded yet" in content

    def test_board_links_to_the_wait_breakdown(self) -> None:
        response = self.client.get(reverse("dash:board"), **_LOOPBACK)
        assert response.status_code == 200
        content = response.content.decode()
        assert "Factory speed" in content
        assert f'href="{reverse("dash:cycle_time")}#merge-waits">See the wait breakdown</a>' in content
        assert "Last 1 merged pull requests: 67% working;" in content
        assert "top wait Review; at least 0.0 recorded hand cancels per merge;" in content
        assert "1.0 inferred hand tasks per merge" in content

    def test_board_has_no_undefined_intervention_rate_without_merges(self) -> None:
        TicketTransition.objects.filter(to_state=State.MERGED).delete()
        body = self.client.get(reverse("dash:board"), **_LOOPBACK).content.decode()
        assert "No merged pull requests to measure yet." in body
        assert "at least unmeasured" not in body

    def test_merge_summary_uses_reliable_time_splits_and_all_recorded_actions(self) -> None:
        base = build_merge_wait_breakdowns()[0]
        unmeasured = replace(
            base,
            measured=False,
            work_seconds=0,
            hand_cancels=4,
            inferred_hand_tasks=10,
            wait_seconds={**base.wait_seconds, "human": 1000 * MINUTE},
        )
        incomplete = replace(
            base,
            parks_retained=False,
            elapsed_seconds=1000 * MINUTE,
            work_seconds=500 * MINUTE,
            hand_cancels=2,
            inferred_hand_tasks=4,
            wait_seconds={**base.wait_seconds, "human": 1000 * MINUTE},
        )
        summary = build_merge_summary((base, unmeasured, incomplete))
        assert summary.merged_count == 3
        assert summary.working_share == "67%"
        assert summary.top_wait == "Review"
        assert summary.interventions_per_merge == "2.0"
        assert summary.inferred_hand_tasks_per_merge == "5.0"

    def test_unmeasured_merge_and_incomplete_park_history_are_labelled(self) -> None:
        task = Task.objects.get(ticket__issue_url="https://example.test/issues/5313")
        Task.objects.filter(pk=task.pk).update(admitted_at=None)
        body = self.client.get(reverse("dash:cycle_time"), **_LOOPBACK).content.decode()
        assert "unmeasured — task admission history is missing or overwritten" in body
        Task.objects.filter(pk=task.pk).update(admitted_at=timezone.now() - timedelta(days=8))
        body = self.client.get(reverse("dash:cycle_time"), **_LOOPBACK).content.decode()
        assert "park history incomplete" in body

    def test_unmeasured_merge_row_spans_all_table_columns(self) -> None:
        task = Task.objects.get(ticket__issue_url="https://example.test/issues/5313")
        Task.objects.filter(pk=task.pk).update(admitted_at=None)
        body = self.client.get(reverse("dash:cycle_time"), **_LOOPBACK).content.decode()
        section = body.split('id="merge-waits"', 1)[1].split("</section>", 1)[0]
        headers = re.findall(r"<th\b[^>]*>", section)
        row = re.findall(r"<tr>(.*?)</tr>", section, re.DOTALL)[1]
        cells = re.findall(r"<td\b([^>]*)>", row)
        assert sum(
            int(re.search(r'colspan="(\d+)"', cell).group(1)) if "colspan=" in cell else 1 for cell in cells
        ) == len(headers)

    def test_intervention_line_separates_recorded_and_inferred_actions(self) -> None:
        task = Task.objects.get(ticket__issue_url="https://example.test/issues/5313")
        TaskAttempt.objects.create(
            task=task,
            ended_at=build_merge_wait_breakdowns()[0].merged_at - timedelta(minutes=1),
            result={"cancel_source": "owner"},
        )
        Task.objects.create(ticket=task.ticket, session=task.session, phase="shipping")
        body = self.client.get(reverse("dash:cycle_time"), **_LOOPBACK).content.decode()
        assert "at least 1.0 recorded hand cancels per merge" in body
        assert "2.0 inferred hand tasks per merge" in body


class CycleTimePageTestCase(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = ticket_taking(minutes=85, phase_cost=1.5)

    def setUp(self) -> None:
        post_save.disconnect(_auto_enqueue_task, sender=Task, dispatch_uid="auto_enqueue_task")
        self.addCleanup(post_save.connect, _auto_enqueue_task, sender=Task, dispatch_uid="auto_enqueue_task")

    def test_the_page_is_in_the_nav(self) -> None:
        assert ("dash:cycle_time", "Cycle time") in NAV_ITEMS

    def test_phase_gap_signal_shows_a_thirty_hour_wait_beside_pull_request_time(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, overlay="t3-teatree")
        prior = Task.objects.create(ticket=ticket, session=session, phase="planning")
        next_task = Task.objects.create(ticket=ticket, session=session, phase="coding")
        end = timezone.now() - timedelta(hours=30)
        Task.objects.filter(pk=prior.pk).update(created_at=end - timedelta(hours=2), status=Task.Status.COMPLETED)
        Task.objects.filter(pk=next_task.pk).update(created_at=end + timedelta(minutes=1))
        TaskAttempt.objects.create(task=prior, ended_at=end, exit_code=0)

        body = self.client.get(reverse("dash:cycle_time"), **_LOOPBACK).content.decode()

        assert "Pull requests: start to merged" in body
        assert "Wait between consecutive ticket phases (1 measured)" in body
        assert "longest <strong>30h</strong>" in body

    def test_the_flow_band_shows_the_merged_median_and_open_ages_oldest_first(self) -> None:
        for hours in (1, 2, 100):
            merged = started_pull_request(hours_ago=hours, state=PullRequest.State.MERGED)
            TicketTransition.objects.create(
                ticket=merged.ticket, from_state=State.REVIEW_REQUESTED, to_state=State.MERGED
            )
        young = started_pull_request(hours_ago=5)
        old = started_pull_request(hours_ago=80)

        body = self.client.get(reverse("dash:cycle_time"), **_LOOPBACK).content.decode()

        assert "Pull requests: start to merged" in body
        assert "Median of 3 merged pull requests: <strong>2h</strong>" in body
        assert body.index(old.url) < body.index(young.url)
        old_row = re.search(rf'<tr>\s*<td><a href="{re.escape(old.url)}">.*?</tr>', body, re.DOTALL)
        assert old_row is not None
        assert '<td class="num">80h <span class="chip warn">72h+</span></td>' in old_row.group()
        young_row = re.search(rf'<tr>\s*<td><a href="{re.escape(young.url)}">.*?</tr>', body, re.DOTALL)
        assert young_row is not None
        assert 'class="chip warn"' not in young_row.group()
        assert re.search(rf'{re.escape(young.url)}</a></td>\s*<td class="num">5h</td>', body)

    def test_the_flow_band_shows_empty_open_and_unmeasured_median_states(self) -> None:
        body = self.client.get(reverse("dash:cycle_time"), **_LOOPBACK).content.decode()

        assert "Median of 0 merged pull requests: <strong>—</strong>" in body
        assert "No open pull requests." in body

    def test_an_open_pull_request_without_a_clock_is_labelled_unmeasured(self) -> None:
        row = PullRequestFactory(ticket=Ticket.objects.create())

        body = self.client.get(reverse("dash:cycle_time"), **_LOOPBACK).content.decode()

        assert re.search(rf'{re.escape(row.url)}</a></td>\s*<td class="num">unmeasured</td>', body)

    def test_stale_pull_request_chip_uses_the_threshold(self) -> None:
        started_pull_request(hours_ago=80)

        with patch("teatree.dash.cycle_time.OPEN_PR_RED_HOURS", 48.0):
            body = self.client.get(reverse("dash:cycle_time"), **_LOOPBACK).content.decode()

        assert '<span class="chip warn">48h+</span>' in body

    def test_the_page_renders_the_transition_the_data_contains(self) -> None:
        response = self.client.get(reverse("dash:cycle_time"), **_LOOPBACK)
        assert response.status_code == 200
        assert f"{State.PLAN_RECORDED} → {State.CODED}" in response.content.decode()

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
