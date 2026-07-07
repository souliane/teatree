"""Integration tests for the read-only structured-search queries.

Each test builds real rows via the factories and asserts the actual filtered
result of the query function — the manager reuse (overlay scoping, in-flight,
resolve) is exercised end to end against the test DB, not mocked.
"""

from pathlib import Path

from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import ConfigSetting, IncomingEvent, PullRequest, Task, Ticket
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


class TestConfigSettingGet(TestCase):
    def test_db_override_reports_db_source(self) -> None:
        ConfigSetting.objects.set_value("factory_score_enabled", value=True)

        row = search.config_setting_get(key="factory_score_enabled")

        assert row["known"] is True
        assert row["value"] is True
        assert row["source"] == "db"
        assert row["scope"] == "global"

    def test_absent_row_falls_through_to_file_env(self) -> None:
        row = search.config_setting_get(key="factory_score_enabled")

        assert row["known"] is True
        assert row["source"] == "file/env"
        assert isinstance(row["value"], bool)

    def test_overlay_scope_row_reports_overlay_scope(self) -> None:
        ConfigSetting.objects.set_value("factory_score_enabled", value=True, scope="t3-teatree")

        row = search.config_setting_get(key="factory_score_enabled", overlay="t3-teatree")

        assert row["source"] == "db"
        assert row["scope"] == "overlay:t3-teatree"
        assert row["overlay"] == "t3-teatree"

    def test_unknown_key_is_flagged_not_raised(self) -> None:
        row = search.config_setting_get(key="not_a_real_setting")

        assert row["known"] is False
        assert row["value"] is None

    def test_path_valued_setting_is_coerced_to_a_string(self) -> None:
        # A Path fallback (workspace_dir) is not JSON-serializable — it must be
        # stringified so the read-only tool never fails at the JSON boundary.
        row = search.config_setting_get(key="workspace_dir")

        assert isinstance(row["value"], str)

    def test_list_valued_setting_round_trips_as_a_list(self) -> None:
        row = search.config_setting_get(key="excluded_skills")

        assert isinstance(row["value"], list)


class TestJsonable:
    def test_primitives_and_none_pass_through(self) -> None:
        assert search._jsonable(None) is None
        assert search._jsonable(value=True) is True
        assert search._jsonable(3) == 3
        assert search._jsonable("x") == "x"

    def test_nested_containers_are_coerced_recursively(self) -> None:
        coerced = search._jsonable({"p": Path("/tmp/x"), "nums": [1, 2]})

        assert coerced == {"p": "/tmp/x", "nums": [1, 2]}

    def test_a_non_json_scalar_is_stringified(self) -> None:
        assert search._jsonable(object()).startswith("<object object")


class TestTicketGet(TestCase):
    def test_resolves_by_pk_and_returns_detail(self) -> None:
        ticket = TicketFactory(issue_url="https://x/issues/500", short_description="detail me")

        row = search.ticket_get(ticket=str(ticket.pk))

        assert row["id"] == ticket.pk
        assert row["short_description"] == "detail me"
        assert row["visited_phases"] == []

    def test_resolves_by_bare_issue_number(self) -> None:
        ticket = TicketFactory(issue_url="https://github.com/souliane/teatree/issues/501")

        row = search.ticket_get(ticket="501")

        assert row["id"] == ticket.pk

    def test_surfaces_visited_phases_from_the_session_ledger(self) -> None:
        ticket = TicketFactory(issue_url="https://x/issues/502")
        SessionFactory(ticket=ticket, visited_phases=["scoping", "planning"])

        row = search.ticket_get(ticket=str(ticket.pk))

        assert row["visited_phases"] == ["scoping", "planning"]

    def test_unknown_ticket_returns_empty_dict(self) -> None:
        assert search.ticket_get(ticket="999999") == {}


class TestTicketList(TestCase):
    def test_filters_by_state(self) -> None:
        coded = TicketFactory(state=Ticket.State.CODED, issue_url="https://x/issues/510")
        TicketFactory(state=Ticket.State.MERGED, issue_url="https://x/issues/511")

        rows = search.ticket_list(state=Ticket.State.CODED)

        assert [row["id"] for row in rows] == [coded.pk]

    def test_in_flight_excludes_delivered(self) -> None:
        live = TicketFactory(state=Ticket.State.STARTED, issue_url="https://x/issues/512")
        TicketFactory(state=Ticket.State.DELIVERED, issue_url="https://x/issues/513")

        ids = {row["id"] for row in search.ticket_list(in_flight=True)}

        assert ids == {live.pk}

    def test_overlay_scopes_the_list(self) -> None:
        mine = TicketFactory(overlay="t3-teatree", issue_url="https://x/issues/514")
        TicketFactory(overlay="other-overlay", issue_url="https://x/issues/515")

        ids = {row["id"] for row in search.ticket_list(overlay="t3-teatree")}

        assert mine.pk in ids


class TestTaskList(TestCase):
    def test_filters_by_status(self) -> None:
        pending = TaskFactory(status=Task.Status.PENDING)
        TaskFactory(status=Task.Status.COMPLETED)

        rows = search.task_list(status=Task.Status.PENDING)

        assert [row["id"] for row in rows] == [pending.pk]
        assert rows[0]["status"] == Task.Status.PENDING

    def test_phase_filter_accepts_any_spelling(self) -> None:
        review = TaskFactory(phase="review")
        TaskFactory(phase="coding")

        ids = {row["id"] for row in search.task_list(phase="reviewing")}

        assert ids == {review.pk}

    def test_scopes_to_a_ticket_reference(self) -> None:
        ticket = TicketFactory(issue_url="https://x/issues/520")
        mine = TaskFactory(ticket=ticket)
        TaskFactory()  # a task on a different ticket

        rows = search.task_list(ticket=str(ticket.pk))

        assert [row["id"] for row in rows] == [mine.pk]

    def test_serializes_ticket_number_and_subject(self) -> None:
        ticket = TicketFactory(issue_url="https://x/issues/521", short_description="do the thing")
        TaskFactory(ticket=ticket, phase="coding")

        row = search.task_list(ticket=str(ticket.pk))[0]

        assert row["ticket_number"] == ticket.ticket_number
        assert row["phase"] == "coding"
        assert row["subject"]

    def test_unknown_ticket_returns_empty(self) -> None:
        assert search.task_list(ticket="999999") == []


class TestGateStatus(TestCase):
    def test_reports_review_and_raw_merge_gate_shape(self) -> None:
        report = search.gate_status()

        assert isinstance(report["review_gate"]["require_human_approval_to_merge"], bool)
        assert isinstance(report["raw_merge_gate"]["out_of_band_merge_gate_enabled"], bool)

    def test_review_gate_reflects_a_config_override(self) -> None:
        call_command("config_setting", "set", "require_human_approval_to_merge", "false")

        report = search.gate_status()

        assert report["review_gate"]["require_human_approval_to_merge"] is False


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
