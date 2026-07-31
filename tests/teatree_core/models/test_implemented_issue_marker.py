"""Durable issue-implementer ledger tests (#1549).

The marker dedupes re-ticks (idempotent ``claim`` keyed on
``(issue_url, overlay)``) and exposes the max-concurrent budget the loop
reads (``in_flight_count`` over non-terminal rows). FK to ``Ticket`` is
``SET_NULL`` so deleting a ticket orphans the marker without losing the
dedup record.
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import ImplementedIssueMarker, PullRequest, Task, Ticket
from teatree.instance_id import instance_id
from tests.factories import ImplementedIssueMarkerFactory, PullRequestFactory, TaskFactory, TicketFactory


class TestClaim(TestCase):
    def test_inserts_first_observation(self) -> None:
        row = ImplementedIssueMarker.objects.claim(
            "https://github.com/o/r/issues/1",
            "acme",
            head_sha="abc123",
        )

        assert row is not None
        assert row.issue_url == "https://github.com/o/r/issues/1"
        assert row.overlay == "acme"
        assert row.head_sha == "abc123"
        assert row.state == ImplementedIssueMarker.State.DISPATCHED

    def test_returns_none_on_duplicate(self) -> None:
        ImplementedIssueMarker.objects.claim("https://github.com/o/r/issues/2", "acme")
        again = ImplementedIssueMarker.objects.claim("https://github.com/o/r/issues/2", "acme")
        assert again is None

    def test_distinct_overlay_is_not_a_duplicate(self) -> None:
        first = ImplementedIssueMarker.objects.claim("https://github.com/o/r/issues/3", "acme")
        other = ImplementedIssueMarker.objects.claim("https://github.com/o/r/issues/3", "widgets")
        assert first is not None
        assert other is not None
        assert first.pk != other.pk

    def test_no_op_on_missing_url(self) -> None:
        assert ImplementedIssueMarker.objects.claim("", "acme") is None

    def test_stamps_the_claiming_instance_id(self) -> None:
        row = ImplementedIssueMarker.objects.claim("https://github.com/o/r/issues/7", "acme")
        assert row is not None
        assert row.claimed_by_instance == instance_id()

    def test_explicit_instance_overrides_the_default(self) -> None:
        row = ImplementedIssueMarker.objects.claim(
            "https://github.com/o/r/issues/8",
            "acme",
            claimed_by_instance="other-box",
        )
        assert row is not None
        assert row.claimed_by_instance == "other-box"


class TestInFlightCount(TestCase):
    def test_counts_dispatched_and_ticket_created_excludes_terminal(self) -> None:
        ImplementedIssueMarkerFactory(overlay="acme")
        ImplementedIssueMarkerFactory(overlay="acme", ticket_created=True)
        ImplementedIssueMarkerFactory(overlay="acme", abandoned=True)
        ImplementedIssueMarkerFactory(overlay="acme", completed=True)

        assert ImplementedIssueMarker.objects.in_flight_count("acme") == 2

    def test_scoped_per_overlay(self) -> None:
        ImplementedIssueMarkerFactory(overlay="acme")
        ImplementedIssueMarkerFactory(overlay="widgets")

        assert ImplementedIssueMarker.objects.in_flight_count("acme") == 1


class TestTicketRelation(TestCase):
    def test_ticket_delete_sets_null(self) -> None:
        ticket = TicketFactory()
        marker = ImplementedIssueMarkerFactory(overlay="acme", ticket=ticket)

        ticket.delete()
        marker.refresh_from_db()

        assert marker.ticket_id is None


class TestStr(TestCase):
    def test_renders_url_and_state(self) -> None:
        marker = ImplementedIssueMarkerFactory(issue_url="https://github.com/o/r/issues/9", ticket_created=True)
        rendered = str(marker)
        assert "impl-issue" in rendered
        assert "https://github.com/o/r/issues/9" in rendered
        assert "ticket_created" in rendered


class TestReconcileStale(TestCase):
    """#3275 — retroactively free markers whose ticket went terminal/gone.

    The release-on-completion signal only fires on the LIVE transition event;
    a marker orphaned while the pipeline was down never leaves ``dispatched``
    and permanently exhausts the in-flight budget. ``reconcile_stale`` is the
    retroactive path that heals it.
    """

    def _terminal_ticket(self, issue_url: str):
        return TicketFactory(overlay="acme", issue_url=issue_url, state=Ticket.State.MERGED)

    def test_releases_dispatched_marker_with_merged_ticket(self) -> None:
        url = "https://github.com/o/r/issues/100"
        self._terminal_ticket(url)
        marker = ImplementedIssueMarkerFactory(overlay="acme", issue_url=url)  # DISPATCHED

        result = ImplementedIssueMarker.objects.reconcile_stale("acme")

        marker.refresh_from_db()
        assert marker.state == ImplementedIssueMarker.State.COMPLETED
        assert result.completed == (marker.pk,)
        assert result.released == 1

    def test_frees_the_in_flight_budget(self) -> None:
        url = "https://github.com/o/r/issues/101"
        self._terminal_ticket(url)
        ImplementedIssueMarkerFactory(overlay="acme", issue_url=url)
        assert ImplementedIssueMarker.objects.in_flight_count("acme") == 1

        ImplementedIssueMarker.objects.reconcile_stale("acme")

        assert ImplementedIssueMarker.objects.in_flight_count("acme") == 0

    def test_keeps_marker_whose_ticket_is_still_live(self) -> None:
        url = "https://github.com/o/r/issues/102"
        TicketFactory(overlay="acme", issue_url=url, state=Ticket.State.CODED)
        marker = ImplementedIssueMarkerFactory(overlay="acme", issue_url=url)

        result = ImplementedIssueMarker.objects.reconcile_stale("acme")

        marker.refresh_from_db()
        assert marker.state == ImplementedIssueMarker.State.DISPATCHED
        assert result.released == 0

    def test_releases_ticket_created_marker_with_delivered_ticket(self) -> None:
        url = "https://github.com/o/r/issues/103"
        TicketFactory(overlay="acme", issue_url=url, state=Ticket.State.DELIVERED)
        marker = ImplementedIssueMarkerFactory(overlay="acme", issue_url=url, ticket_created=True)

        ImplementedIssueMarker.objects.reconcile_stale("acme")

        marker.refresh_from_db()
        assert marker.state == ImplementedIssueMarker.State.COMPLETED

    def test_abandons_orphan_with_gone_ticket_past_grace(self) -> None:
        marker = ImplementedIssueMarkerFactory(overlay="acme", issue_url="https://github.com/o/r/issues/104")
        ImplementedIssueMarker.objects.filter(pk=marker.pk).update(dispatched_at=timezone.now() - timedelta(hours=48))

        result = ImplementedIssueMarker.objects.reconcile_stale("acme")

        marker.refresh_from_db()
        assert marker.state == ImplementedIssueMarker.State.ABANDONED
        assert result.abandoned == (marker.pk,)

    def test_keeps_fresh_orphan_within_grace(self) -> None:
        marker = ImplementedIssueMarkerFactory(overlay="acme", issue_url="https://github.com/o/r/issues/105")

        result = ImplementedIssueMarker.objects.reconcile_stale("acme")

        marker.refresh_from_db()
        assert marker.state == ImplementedIssueMarker.State.DISPATCHED
        assert result.released == 0

    def test_leaves_already_terminal_markers_untouched(self) -> None:
        url = "https://github.com/o/r/issues/106"
        self._terminal_ticket(url)
        completed = ImplementedIssueMarkerFactory(overlay="acme", issue_url=url, completed=True)
        abandoned = ImplementedIssueMarkerFactory(
            overlay="acme", issue_url="https://github.com/o/r/issues/107", abandoned=True
        )

        result = ImplementedIssueMarker.objects.reconcile_stale("acme")

        completed.refresh_from_db()
        abandoned.refresh_from_db()
        assert completed.state == ImplementedIssueMarker.State.COMPLETED
        assert abandoned.state == ImplementedIssueMarker.State.ABANDONED
        assert result.released == 0

    def test_scoped_to_overlay(self) -> None:
        url_a = "https://github.com/o/r/issues/108"
        url_b = "https://github.com/o/r/issues/109"
        self._terminal_ticket(url_a)
        TicketFactory(overlay="widgets", issue_url=url_b, state=Ticket.State.MERGED)
        acme_marker = ImplementedIssueMarkerFactory(overlay="acme", issue_url=url_a)
        widgets_marker = ImplementedIssueMarkerFactory(overlay="widgets", issue_url=url_b)

        ImplementedIssueMarker.objects.reconcile_stale("acme")

        acme_marker.refresh_from_db()
        widgets_marker.refresh_from_db()
        assert acme_marker.state == ImplementedIssueMarker.State.COMPLETED
        assert widgets_marker.state == ImplementedIssueMarker.State.DISPATCHED

    def test_empty_overlay_reconciles_every_overlay(self) -> None:
        url_a = "https://github.com/o/r/issues/110"
        url_b = "https://github.com/o/r/issues/111"
        self._terminal_ticket(url_a)
        TicketFactory(overlay="widgets", issue_url=url_b, state=Ticket.State.MERGED)
        acme_marker = ImplementedIssueMarkerFactory(overlay="acme", issue_url=url_a)
        widgets_marker = ImplementedIssueMarkerFactory(overlay="widgets", issue_url=url_b)

        result = ImplementedIssueMarker.objects.reconcile_stale()

        acme_marker.refresh_from_db()
        widgets_marker.refresh_from_db()
        assert acme_marker.state == ImplementedIssueMarker.State.COMPLETED
        assert widgets_marker.state == ImplementedIssueMarker.State.COMPLETED
        assert result.released == 2

    def test_find_stale_previews_without_mutating(self) -> None:
        url = "https://github.com/o/r/issues/112"
        self._terminal_ticket(url)
        marker = ImplementedIssueMarkerFactory(overlay="acme", issue_url=url)

        result = ImplementedIssueMarker.objects.find_stale("acme")

        marker.refresh_from_db()
        assert marker.state == ImplementedIssueMarker.State.DISPATCHED
        assert result.completed == (marker.pk,)


class TestReconcileStalledTicket(TestCase):
    """A marker whose ticket EXISTS but died mid-pipeline must still release.

    The #3275 reconciler covered only the two ends of the range — a ticket that
    reached a terminal state, and a ticket that never existed. The gap between
    them is a ticket that WAS created and then stopped: every task FAILED, none
    pending, the state frozen short of terminal. Such a marker is non-terminal
    forever, so it holds an in-flight slot for good; enough of them and
    ``issue_implementer_max_concurrent`` is permanently exhausted and the intake
    scanner is never even built — the factory reads enabled and implements
    nothing.
    """

    def _stalled(self, url: str, *, state: str = Ticket.State.PLANNED, age_hours: int = 72) -> ImplementedIssueMarker:
        ticket = TicketFactory(overlay="acme", issue_url=url, state=state)
        task = TaskFactory(ticket=ticket, status=Task.Status.FAILED)
        Task.objects.filter(pk=task.pk).update(created_at=timezone.now() - timedelta(hours=age_hours))
        marker = ImplementedIssueMarkerFactory(overlay="acme", issue_url=url, ticket_created=True, ticket=ticket)
        ImplementedIssueMarker.objects.filter(pk=marker.pk).update(
            dispatched_at=timezone.now() - timedelta(hours=age_hours)
        )
        return ImplementedIssueMarker.objects.get(pk=marker.pk)

    def test_abandons_marker_whose_ticket_stalled_past_grace(self) -> None:
        marker = self._stalled("https://github.com/o/r/issues/200")

        result = ImplementedIssueMarker.objects.reconcile_stale("acme")

        marker.refresh_from_db()
        assert marker.state == ImplementedIssueMarker.State.ABANDONED
        assert result.abandoned == (marker.pk,)

    def test_frees_the_in_flight_budget(self) -> None:
        self._stalled("https://github.com/o/r/issues/201")
        assert ImplementedIssueMarker.objects.in_flight_count("acme") == 1

        ImplementedIssueMarker.objects.reconcile_stale("acme")

        assert ImplementedIssueMarker.objects.in_flight_count("acme") == 0

    def test_released_issue_is_claimable_again(self) -> None:
        url = "https://github.com/o/r/issues/202"
        self._stalled(url)

        ImplementedIssueMarker.objects.reconcile_stale("acme")

        assert ImplementedIssueMarker.objects.claim(url, "acme") is not None

    def test_keeps_a_ticket_with_an_active_task(self) -> None:
        url = "https://github.com/o/r/issues/203"
        marker = self._stalled(url)
        pending = TaskFactory(ticket=marker.ticket, status=Task.Status.PENDING)
        Task.objects.filter(pk=pending.pk).update(created_at=timezone.now() - timedelta(hours=72))

        result = ImplementedIssueMarker.objects.reconcile_stale("acme")

        marker.refresh_from_db()
        assert marker.state == ImplementedIssueMarker.State.TICKET_CREATED
        assert result.released == 0

    def test_keeps_a_ticket_whose_work_is_recent(self) -> None:
        marker = self._stalled("https://github.com/o/r/issues/204", age_hours=1)

        result = ImplementedIssueMarker.objects.reconcile_stale("acme")

        marker.refresh_from_db()
        assert marker.state == ImplementedIssueMarker.State.TICKET_CREATED
        assert result.released == 0

    def test_keeps_a_taskless_ticket_within_the_dispatch_grace(self) -> None:
        url = "https://github.com/o/r/issues/205"
        TicketFactory(overlay="acme", issue_url=url, state=Ticket.State.STARTED)
        marker = ImplementedIssueMarkerFactory(overlay="acme", issue_url=url)

        result = ImplementedIssueMarker.objects.reconcile_stale("acme")

        marker.refresh_from_db()
        assert marker.state == ImplementedIssueMarker.State.DISPATCHED
        assert result.released == 0

    def test_find_stale_previews_the_stall_without_mutating(self) -> None:
        marker = self._stalled("https://github.com/o/r/issues/206")

        result = ImplementedIssueMarker.objects.find_stale("acme")

        marker.refresh_from_db()
        assert marker.state == ImplementedIssueMarker.State.TICKET_CREATED
        assert result.abandoned == (marker.pk,)


class TestReleaseOnMergedPr(TestCase):
    """A claim whose PR has LANDED must not wait out a grace for a step the ticket will never take.

    SHIPPED is deliberately not a ``marker_release_states()`` member — it means "PR open,
    not yet landed" — so a ticket frozen at SHIPPED after its PR merged satisfies no
    release condition and holds its slot for the full 24h stall grace. At the shipped
    budget two of those close intake for a day.
    """

    def _shipped_with_merged_pr(self, url: str) -> ImplementedIssueMarker:
        ticket = TicketFactory(overlay="acme", issue_url=url, state=Ticket.State.SHIPPED)
        PullRequestFactory(ticket=ticket, overlay="acme", state=PullRequest.State.MERGED)
        marker = ImplementedIssueMarkerFactory(overlay="acme", issue_url=url, ticket_created=True, ticket=ticket)
        return ImplementedIssueMarker.objects.get(pk=marker.pk)

    def test_merged_pr_releases_a_fresh_claim(self) -> None:
        marker = self._shipped_with_merged_pr("https://github.com/o/r/issues/300")

        result = ImplementedIssueMarker.objects.reconcile_stale("acme")

        marker.refresh_from_db()
        assert marker.state == ImplementedIssueMarker.State.COMPLETED
        assert result.completed == (marker.pk,)

    def test_merged_pr_frees_the_in_flight_budget(self) -> None:
        self._shipped_with_merged_pr("https://github.com/o/r/issues/301")
        assert ImplementedIssueMarker.objects.in_flight_count("acme") == 1

        ImplementedIssueMarker.objects.reconcile_stale("acme")

        assert ImplementedIssueMarker.objects.in_flight_count("acme") == 0

    def test_merged_pr_releases_even_with_an_active_task(self) -> None:
        # The PR landed; a pending retro/delivery task is not a reason to hold intake budget.
        marker = self._shipped_with_merged_pr("https://github.com/o/r/issues/302")
        TaskFactory(ticket=marker.ticket, status=Task.Status.PENDING)

        result = ImplementedIssueMarker.objects.reconcile_stale("acme")

        marker.refresh_from_db()
        assert marker.state == ImplementedIssueMarker.State.COMPLETED
        assert result.completed == (marker.pk,)

    def test_open_pr_does_not_release(self) -> None:
        url = "https://github.com/o/r/issues/303"
        ticket = TicketFactory(overlay="acme", issue_url=url, state=Ticket.State.SHIPPED)
        PullRequestFactory(ticket=ticket, overlay="acme", state=PullRequest.State.OPEN)
        marker = ImplementedIssueMarkerFactory(overlay="acme", issue_url=url, ticket_created=True, ticket=ticket)

        result = ImplementedIssueMarker.objects.reconcile_stale("acme")

        marker.refresh_from_db()
        assert marker.state == ImplementedIssueMarker.State.TICKET_CREATED
        assert result.released == 0


class TestDeadClaimGrace(TestCase):
    """An attempt with nothing queued and nothing opened is over, not slow.

    The 24h stall grace assumes the attempt might still be mid-flight. A ticket whose
    only task failed at once and which never opened a PR shows no sign of an attempt at
    all, so it is held to the much shorter dead-claim grace instead.
    """

    def _dead(self, url: str, *, age_hours: int) -> ImplementedIssueMarker:
        ticket = TicketFactory(overlay="acme", issue_url=url, state=Ticket.State.NOT_STARTED)
        task = TaskFactory(ticket=ticket, status=Task.Status.FAILED)
        Task.objects.filter(pk=task.pk).update(created_at=timezone.now() - timedelta(hours=age_hours))
        marker = ImplementedIssueMarkerFactory(overlay="acme", issue_url=url, ticket_created=True, ticket=ticket)
        ImplementedIssueMarker.objects.filter(pk=marker.pk).update(
            dispatched_at=timezone.now() - timedelta(hours=age_hours)
        )
        return ImplementedIssueMarker.objects.get(pk=marker.pk)

    def test_dead_attempt_releases_long_before_the_stall_grace(self) -> None:
        marker = self._dead("https://github.com/o/r/issues/310", age_hours=4)

        result = ImplementedIssueMarker.objects.reconcile_stale("acme")

        marker.refresh_from_db()
        assert marker.state == ImplementedIssueMarker.State.ABANDONED
        assert result.abandoned == (marker.pk,)

    def test_dead_attempt_within_the_short_grace_is_kept(self) -> None:
        marker = self._dead("https://github.com/o/r/issues/311", age_hours=1)

        result = ImplementedIssueMarker.objects.reconcile_stale("acme")

        marker.refresh_from_db()
        assert marker.state == ImplementedIssueMarker.State.TICKET_CREATED
        assert result.released == 0

    def test_open_pr_keeps_the_long_stall_grace(self) -> None:
        # Awaiting human review IS mid-flight — the 24h grace still governs it.
        url = "https://github.com/o/r/issues/312"
        marker = self._dead(url, age_hours=4)
        PullRequestFactory(ticket=marker.ticket, overlay="acme", state=PullRequest.State.REVIEW_REQUESTED)

        result = ImplementedIssueMarker.objects.reconcile_stale("acme")

        marker.refresh_from_db()
        assert marker.state == ImplementedIssueMarker.State.TICKET_CREATED
        assert result.released == 0

    def test_the_dead_grace_never_outlasts_the_stall_grace(self) -> None:
        # An operator shortening the stall grace must not find PR-less claims held
        # LONGER than mid-flight ones — the dead branch only ever releases sooner.
        marker = self._dead("https://github.com/o/r/issues/314", age_hours=0)

        result = ImplementedIssueMarker.objects.reconcile_stale("acme", stall_grace=timedelta(0))

        marker.refresh_from_db()
        assert marker.state == ImplementedIssueMarker.State.ABANDONED
        assert result.abandoned == (marker.pk,)

    def test_active_task_keeps_the_slot_inside_the_short_grace(self) -> None:
        marker = self._dead("https://github.com/o/r/issues/313", age_hours=4)
        TaskFactory(ticket=marker.ticket, status=Task.Status.CLAIMED)

        result = ImplementedIssueMarker.objects.reconcile_stale("acme")

        marker.refresh_from_db()
        assert marker.state == ImplementedIssueMarker.State.TICKET_CREATED
        assert result.released == 0
