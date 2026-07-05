"""Integration tests for the read-only structured-search queries.

Each test builds real rows via the factories and asserts the actual filtered
result of the query function — the manager reuse (overlay scoping, in-flight,
resolve) is exercised end to end against the test DB, not mocked.
"""

from django.test import TestCase

from teatree.core.models import IncomingEvent, PullRequest, Task, Ticket
from teatree.mcp import search
from tests.factories import (
    IncomingEventFactory,
    PullRequestFactory,
    ReplyDispatchFactory,
    SessionFactory,
    TaskFactory,
    TicketFactory,
    WorktreeFactory,
)


class TestCapped:
    def test_non_positive_falls_back_to_default(self) -> None:
        assert search._capped(0, 50) == 50
        assert search._capped(-7, 20) == 20

    def test_in_range_limit_passes_through(self) -> None:
        assert search._capped(10, 50) == 10

    def test_oversized_limit_is_clamped_to_max(self) -> None:
        assert search._capped(10_000, 50) == search._MAX_LIMIT


class TestTicketSearch(TestCase):
    def test_overlay_scopes_and_includes_legacy_empty_overlay(self) -> None:
        mine = TicketFactory(overlay="t3-teatree", issue_url="https://x/issues/1")
        legacy = TicketFactory(overlay="", issue_url="https://x/issues/2")
        TicketFactory(overlay="other-overlay", issue_url="https://x/issues/3")

        ids = {row["id"] for row in search.ticket_search(overlay="t3-teatree")}

        assert ids == {mine.pk, legacy.pk}

    def test_filters_by_state(self) -> None:
        coded = TicketFactory(state=Ticket.State.CODED, issue_url="https://x/issues/10")
        TicketFactory(state=Ticket.State.MERGED, issue_url="https://x/issues/11")

        rows = search.ticket_search(state=Ticket.State.CODED)

        assert [row["id"] for row in rows] == [coded.pk]
        assert rows[0]["state"] == Ticket.State.CODED

    def test_filters_by_kind_and_role(self) -> None:
        target = TicketFactory(
            kind=Ticket.Kind.FIX,
            role=Ticket.Role.REVIEWER,
            issue_url="https://x/issues/20",
        )
        TicketFactory(kind=Ticket.Kind.FEATURE, role=Ticket.Role.AUTHOR, issue_url="https://x/issues/21")

        rows = search.ticket_search(kind=Ticket.Kind.FIX, role=Ticket.Role.REVIEWER)

        assert [row["id"] for row in rows] == [target.pk]

    def test_text_matches_url_description_and_context(self) -> None:
        by_url = TicketFactory(issue_url="https://x/issues/needle-30")
        by_desc = TicketFactory(short_description="a needle in the desc", issue_url="https://x/issues/31")
        by_ctx = TicketFactory(context="buried needle here", issue_url="https://x/issues/32")
        TicketFactory(short_description="unrelated", issue_url="https://x/issues/33")

        ids = {row["id"] for row in search.ticket_search(text="needle")}

        assert ids == {by_url.pk, by_desc.pk, by_ctx.pk}

    def test_in_flight_excludes_delivered_and_ignored(self) -> None:
        live = TicketFactory(state=Ticket.State.STARTED, issue_url="https://x/issues/40")
        TicketFactory(state=Ticket.State.DELIVERED, issue_url="https://x/issues/41")
        TicketFactory(state=Ticket.State.IGNORED, issue_url="https://x/issues/42")

        ids = {row["id"] for row in search.ticket_search(in_flight=True)}

        assert ids == {live.pk}

    def test_orders_newest_first_and_caps_limit(self) -> None:
        created = [TicketFactory(issue_url=f"https://x/issues/5{n}") for n in range(5)]

        rows = search.ticket_search(limit=2)

        assert [row["id"] for row in rows] == [created[-1].pk, created[-2].pk]


class TestWorktreeStatus(TestCase):
    def test_by_ticket_reference_resolves_pk(self) -> None:
        ticket = TicketFactory(issue_url="https://x/issues/60")
        worktree = WorktreeFactory(ticket=ticket)
        WorktreeFactory()  # unrelated worktree on a different ticket

        rows = search.worktree_status(ticket=str(ticket.pk))

        assert [row["id"] for row in rows] == [worktree.pk]
        assert rows[0]["ticket_number"] == ticket.ticket_number

    def test_by_bare_issue_number(self) -> None:
        ticket = TicketFactory(issue_url="https://github.com/souliane/teatree/issues/466")
        worktree = WorktreeFactory(ticket=ticket)

        rows = search.worktree_status(ticket="466")

        assert [row["id"] for row in rows] == [worktree.pk]

    def test_unknown_ticket_returns_empty(self) -> None:
        assert search.worktree_status(ticket="999999") == []

    def test_active_only_excludes_delivered_ticket_worktrees(self) -> None:
        live = WorktreeFactory(ticket=TicketFactory(state=Ticket.State.STARTED, issue_url="https://x/issues/70"))
        WorktreeFactory(ticket=TicketFactory(state=Ticket.State.DELIVERED, issue_url="https://x/issues/71"))

        ids = {row["id"] for row in search.worktree_status(active_only=True)}

        assert ids == {live.pk}


class TestPrForTicket(TestCase):
    def test_returns_all_prs_for_ticket_newest_first(self) -> None:
        ticket = TicketFactory(issue_url="https://x/issues/80")
        first = PullRequestFactory(ticket=ticket)
        second = PullRequestFactory(ticket=ticket)
        PullRequestFactory(ticket=TicketFactory(issue_url="https://x/issues/81"))  # other ticket

        rows = search.pr_for_ticket(ticket=str(ticket.pk))

        assert [row["id"] for row in rows] == [second.pk, first.pk]
        assert rows[0]["state"] == PullRequest.State.OPEN

    def test_unknown_ticket_returns_empty(self) -> None:
        assert search.pr_for_ticket(ticket="999999") == []


class TestLoopStats(TestCase):
    def test_counts_tasks_by_status(self) -> None:
        TaskFactory(status=Task.Status.PENDING)
        TaskFactory(status=Task.Status.PENDING)
        TaskFactory(status=Task.Status.CLAIMED)
        TaskFactory(status=Task.Status.COMPLETED)
        TaskFactory(status=Task.Status.FAILED)

        stats = search.loop_stats()

        assert stats["tasks"] == {"pending": 2, "claimed": 1, "completed": 1, "failed": 1}

    def test_overlay_scopes_task_counts(self) -> None:
        TaskFactory(status=Task.Status.PENDING)  # default overlay t3-teatree
        other_ticket = TicketFactory(overlay="other", issue_url="https://x/issues/90")
        other_session = SessionFactory(ticket=other_ticket, overlay="other")
        TaskFactory(status=Task.Status.PENDING, ticket=other_ticket, session=other_session)

        scoped = search.loop_stats(overlay="t3-teatree")

        assert scoped["overlay"] == "t3-teatree"
        assert scoped["tasks"]["pending"] == 1

    def test_dead_letter_counts_exhausted_dispatches(self) -> None:
        ReplyDispatchFactory(dead=True)
        ReplyDispatchFactory(dead=True)
        ReplyDispatchFactory()  # sent, not dead-lettered

        assert search.loop_stats()["dead_letter"] == 2


class TestFactorySignals(TestCase):
    def test_returns_the_five_signal_report_shape(self) -> None:
        report = search.factory_signals()

        assert report["window_days"] == 28
        assert report["verdict"] in {"ok", "regressing", "red"}
        assert {row["provider_id"] for row in report["signals"]} == {
            "first_try_green",
            "defect_escape",
            "review_catch",
            "merge_latency",
            "repair_burn",
        }

    def test_window_days_flows_through(self) -> None:
        assert search.factory_signals(window_days=14)["window_days"] == 14


class TestFactoryScore(TestCase):
    def test_returns_the_score_payload_shape(self) -> None:
        # Read-only compute is allowed regardless of the flag (calibration path).
        payload = search.factory_score()

        assert payload["verdict"] in {"ok", "regressing", "red"}
        assert "recipe_sha" in payload
        assert "recipe_approved" in payload
        assert {row["provider_id"] for row in payload["signals"]} == {
            "first_try_green",
            "defect_escape",
            "review_catch",
            "merge_latency",
            "repair_burn",
        }

    def test_window_days_flows_through(self) -> None:
        assert search.factory_score(window_days=14)["window_days"] == 14


class TestIncomingEventRecent(TestCase):
    def test_orders_newest_first_and_caps_limit(self) -> None:
        events = [IncomingEventFactory() for _ in range(4)]

        rows = search.incoming_event_recent(limit=2)

        assert [row["id"] for row in rows] == [events[-1].pk, events[-2].pk]

    def test_filters_by_source(self) -> None:
        slack = IncomingEventFactory(source=IncomingEvent.Source.SLACK)
        IncomingEventFactory(source=IncomingEvent.Source.GITHUB)

        rows = search.incoming_event_recent(source=IncomingEvent.Source.SLACK)

        assert [row["id"] for row in rows] == [slack.pk]

    def test_unprocessed_only_excludes_processed(self) -> None:
        pending = IncomingEventFactory()
        processed = IncomingEventFactory()
        processed.mark_processed()

        ids = {row["id"] for row in search.incoming_event_recent(unprocessed_only=True)}

        assert ids == {pending.pk}
