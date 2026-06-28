"""Tests for the pure model -> JSON-dict serializers."""

from django.test import TestCase

from teatree.core.models import IncomingEvent, PullRequest
from teatree.mcp import serializers
from tests.factories import IncomingEventFactory, PullRequestFactory, TicketFactory, WorktreeFactory


class TestSerializeTicket(TestCase):
    def test_projects_search_fields_and_omits_extra(self) -> None:
        ticket = TicketFactory(
            issue_url="https://github.com/souliane/teatree/issues/466",
            short_description="expose MCP",
            extra={"secret_internal_blob": "x"},
        )

        data = serializers.serialize_ticket(ticket)

        assert data["id"] == ticket.pk
        assert data["ticket_number"] == "466"
        assert data["issue_url"].endswith("/466")
        assert data["state"] == ticket.state
        assert "extra" not in data

    def test_json_safe_primitives(self) -> None:
        data = serializers.serialize_ticket(TicketFactory(issue_url="https://x/issues/1"))
        assert isinstance(data["repos"], list)
        assert isinstance(data["is_terminal"], bool)


class TestSerializeWorktree(TestCase):
    def test_reads_ticket_number_through_relation(self) -> None:
        ticket = TicketFactory(issue_url="https://github.com/souliane/teatree/issues/77")
        worktree = WorktreeFactory(ticket=ticket, branch="feat/mcp")

        data = serializers.serialize_worktree(worktree)

        assert data["ticket_id"] == ticket.pk
        assert data["ticket_number"] == "77"
        assert data["branch"] == "feat/mcp"
        assert data["is_stale"] is False  # no on-disk path claimed -> not stale


class TestSerializePullRequest(TestCase):
    def test_projects_pr_fields(self) -> None:
        pr = PullRequestFactory(state=PullRequest.State.MERGED, repo="souliane/teatree")

        data = serializers.serialize_pull_request(pr)

        assert data["repo"] == "souliane/teatree"
        assert data["state"] == PullRequest.State.MERGED
        assert data["review_requested_at"] is None


class TestSerializeIncomingEvent(TestCase):
    def test_projects_event_and_iso_dates(self) -> None:
        event = IncomingEventFactory(source=IncomingEvent.Source.GITLAB, body="hi")

        data = serializers.serialize_incoming_event(event)

        assert data["source"] == IncomingEvent.Source.GITLAB
        assert data["body"] == "hi"
        assert data["is_thread_reply"] is False
        assert "T" in data["received_at"]  # ISO-8601
        assert data["processed_at"] is None
