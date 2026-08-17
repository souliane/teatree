"""The in-flight intake budget, read once and made legible (#3978).

At a full budget the intake scanner factory returns ``None``, so the tick does nothing
and reports success — enabled loop, advancing last-run stamp, no error, no surface
anywhere saying intake is at budget and claiming nothing. These tests pin the reading
that surface is built from, and the predicate that separates "busy" from "deadlocked".
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.core.intake.budget import read_intake_budget, release_deadlocked_holder
from teatree.core.models import ImplementedIssueMarker, PullRequest, Task, Ticket
from tests.factories import ImplementedIssueMarkerFactory, PullRequestFactory, TaskFactory, TicketFactory


def _aged_marker(url: str, *, hours: int = 6, **kw: object) -> ImplementedIssueMarker:
    """A marker dispatched *hours* ago — old enough to be judged past the settle window."""
    marker = ImplementedIssueMarkerFactory(overlay="acme", issue_url=url, ticket_created=True, **kw)
    ImplementedIssueMarker.objects.filter(pk=marker.pk).update(dispatched_at=timezone.now() - timedelta(hours=hours))
    return ImplementedIssueMarker.objects.get(pk=marker.pk)


class TestBudgetOccupancy(TestCase):
    def test_no_markers_is_not_at_budget(self) -> None:
        budget = read_intake_budget("acme", 2)

        assert budget.in_flight == 0
        assert budget.at_budget is False
        assert budget.deadlocked is False

    def test_below_the_limit_is_not_at_budget(self) -> None:
        _aged_marker("https://github.com/o/r/issues/1")

        assert read_intake_budget("acme", 2).at_budget is False

    def test_terminal_markers_do_not_occupy_a_slot(self) -> None:
        ImplementedIssueMarkerFactory(overlay="acme", issue_url="https://github.com/o/r/issues/2", completed=True)
        ImplementedIssueMarkerFactory(overlay="acme", issue_url="https://github.com/o/r/issues/3", abandoned=True)

        assert read_intake_budget("acme", 2).in_flight == 0

    def test_scoped_to_the_overlay(self) -> None:
        _aged_marker("https://github.com/o/r/issues/4")
        ImplementedIssueMarkerFactory(overlay="other", issue_url="https://github.com/o/r/issues/5")

        assert read_intake_budget("acme", 2).in_flight == 1

    def test_an_empty_budget_at_a_zero_limit_is_never_deadlocked(self) -> None:
        # A zero limit means intake is switched off, not jammed — no slot is held.
        assert read_intake_budget("acme", 0).deadlocked is False


class TestProgressPredicate(TestCase):
    """Only a claim showing NO sign of progress makes a full budget a deadlock."""

    def _held(self, url: str, *, state: str = Ticket.State.STARTED, hours: int = 6) -> ImplementedIssueMarker:
        ticket = TicketFactory(overlay="acme", issue_url=url, state=state)
        return _aged_marker(url, hours=hours, ticket=ticket)

    def test_an_active_task_is_progress(self) -> None:
        marker = self._held("https://github.com/o/r/issues/10")
        TaskFactory(ticket=marker.ticket, status=Task.Status.CLAIMED)

        budget = read_intake_budget("acme", 1)

        assert budget.at_budget is True
        assert budget.deadlocked is False

    def test_an_open_pr_is_progress(self) -> None:
        marker = self._held("https://github.com/o/r/issues/11", state=Ticket.State.SHIPPED)
        PullRequestFactory(ticket=marker.ticket, overlay="acme", state=PullRequest.State.REVIEW_REQUESTED)

        assert read_intake_budget("acme", 1).deadlocked is False

    def test_a_failed_attempt_with_no_pr_is_not_progress(self) -> None:
        marker = self._held("https://github.com/o/r/issues/12", state=Ticket.State.NOT_STARTED)
        TaskFactory(ticket=marker.ticket, status=Task.Status.FAILED)

        budget = read_intake_budget("acme", 1)

        assert budget.deadlocked is True
        assert budget.holders[0].ticket_state == Ticket.State.NOT_STARTED

    def test_a_merged_pr_is_not_progress(self) -> None:
        # The observed jam: SHIPPED with a landed PR holds a slot it no longer owns.
        marker = self._held("https://github.com/o/r/issues/13", state=Ticket.State.SHIPPED)
        PullRequestFactory(ticket=marker.ticket, overlay="acme", state=PullRequest.State.MERGED)

        assert read_intake_budget("acme", 1).deadlocked is True

    def test_a_missing_ticket_is_not_progress(self) -> None:
        _aged_marker("https://github.com/o/r/issues/14")

        budget = read_intake_budget("acme", 1)

        assert budget.deadlocked is True
        assert budget.holders[0].ticket_state == ""

    def test_a_fresh_claim_is_too_young_to_judge(self) -> None:
        # A dispatch lands its ticket and first task seconds after the claim; judging it
        # inside that window would read every healthy claim as stuck.
        ImplementedIssueMarkerFactory(overlay="acme", issue_url="https://github.com/o/r/issues/15")

        assert read_intake_budget("acme", 1).deadlocked is False

    def test_one_progressing_holder_clears_the_whole_budget(self) -> None:
        alive = self._held("https://github.com/o/r/issues/16")
        TaskFactory(ticket=alive.ticket, status=Task.Status.PENDING)
        self._held("https://github.com/o/r/issues/17", state=Ticket.State.NOT_STARTED)

        budget = read_intake_budget("acme", 2)

        assert budget.at_budget is True
        assert budget.deadlocked is False


class TestReport(TestCase):
    def test_names_the_overlay_occupancy_and_every_holder(self) -> None:
        url = "https://github.com/o/r/issues/20"
        ticket = TicketFactory(overlay="acme", issue_url=url, state=Ticket.State.SHIPPED)
        _aged_marker(url, ticket=ticket)

        report = read_intake_budget("acme", 1).report()

        assert "acme" in report
        assert "1/1" in report
        assert url in report
        assert Ticket.State.SHIPPED in report

    def test_names_the_static_ceiling_the_live_limit_overrode(self) -> None:
        # The hour-losing trap: `issue_implementer_max_concurrent` reads authoritative
        # while the resource loop's adaptive number is what the gate actually enforces,
        # so raising the setting alone changes nothing and nothing says so.
        _aged_marker("https://github.com/o/r/issues/21")

        report = read_intake_budget("acme", 3, static_limit=4).report()

        assert "1/3" in report
        assert "issue_implementer_max_concurrent is 4" in report

    def test_says_nothing_extra_when_the_limits_agree(self) -> None:
        _aged_marker("https://github.com/o/r/issues/22")

        report = read_intake_budget("acme", 3, static_limit=3).report()

        assert "issue_implementer_max_concurrent" not in report


class TestReleaseDeadlockedHolder(TestCase):
    """A deadlocked budget is acted on, not merely reported (#4389).

    ``deadlocked`` was computed and then only ever read by a doctor check, so a budget
    held entirely by claims going nowhere sat there until each holder's own grace
    expired — hours in which no issue could be admitted and no holder could progress.
    Releasing the longest-held slot is strictly better than waiting: nothing is running
    to protect, and the release is bounded because freeing one slot clears the deadlock.
    """

    def _stuck(self, url: str, *, hours: int) -> ImplementedIssueMarker:
        """A holder past the settle window with no active task and no open PR."""
        ticket = TicketFactory(overlay="acme", issue_url=url, state=Ticket.State.NOT_STARTED)
        return _aged_marker(url, hours=hours, ticket=ticket)

    def test_releases_the_longest_held_slot(self) -> None:
        oldest = self._stuck("https://github.com/o/r/issues/30", hours=9)
        newer = self._stuck("https://github.com/o/r/issues/31", hours=6)
        budget = read_intake_budget("acme", 2)
        assert budget.deadlocked is True

        released = release_deadlocked_holder(budget)

        oldest.refresh_from_db()
        newer.refresh_from_db()
        assert released is not None
        assert released.issue_url == oldest.issue_url
        assert oldest.state == ImplementedIssueMarker.State.ABANDONED
        assert newer.state == ImplementedIssueMarker.State.TICKET_CREATED

    def test_the_freed_slot_makes_the_budget_claimable_again(self) -> None:
        self._stuck("https://github.com/o/r/issues/32", hours=9)
        self._stuck("https://github.com/o/r/issues/33", hours=6)

        release_deadlocked_holder(read_intake_budget("acme", 2))

        assert read_intake_budget("acme", 2).at_budget is False

    def test_a_progressing_budget_is_left_alone(self) -> None:
        alive = self._stuck("https://github.com/o/r/issues/34", hours=9)
        TaskFactory(ticket=alive.ticket, status=Task.Status.PENDING)

        assert release_deadlocked_holder(read_intake_budget("acme", 1)) is None

        alive.refresh_from_db()
        assert alive.state == ImplementedIssueMarker.State.TICKET_CREATED

    def test_a_budget_with_room_is_left_alone(self) -> None:
        holder = self._stuck("https://github.com/o/r/issues/35", hours=9)

        assert release_deadlocked_holder(read_intake_budget("acme", 2)) is None

        holder.refresh_from_db()
        assert holder.state == ImplementedIssueMarker.State.TICKET_CREATED

    def test_a_second_pass_releases_nothing_more(self) -> None:
        # Idempotent against a re-tick: the first release cleared the deadlock, so the
        # remaining holder keeps the grace its own class is entitled to.
        self._stuck("https://github.com/o/r/issues/36", hours=9)
        self._stuck("https://github.com/o/r/issues/37", hours=6)
        release_deadlocked_holder(read_intake_budget("acme", 2))

        assert release_deadlocked_holder(read_intake_budget("acme", 2)) is None

    def test_a_re_claimed_holder_is_not_the_longest_held(self) -> None:
        # `dispatched_at` is the ordering key, not the row id: a re-claim resets the
        # clock in place, so pk order would pick the freshest attempt first.
        reclaimed = self._stuck("https://github.com/o/r/issues/38", hours=9)
        older = self._stuck("https://github.com/o/r/issues/39", hours=7)
        ImplementedIssueMarker.objects.filter(pk=reclaimed.pk).update(dispatched_at=timezone.now() - timedelta(hours=1))

        released = release_deadlocked_holder(read_intake_budget("acme", 2))

        assert released is not None
        assert released.issue_url == older.issue_url
