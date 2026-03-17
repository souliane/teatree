"""Tests for ticket delivery lifecycle state machine."""

import json
from pathlib import Path

import pytest
from lib.fsm import InvalidTransitionError
from lib.ticket_fsm import TicketLifecycle


@pytest.fixture
def ticket_dir(tmp_path: Path) -> Path:
    td = tmp_path / "tickets" / "ac-1234"
    td.mkdir(parents=True)
    return td


@pytest.fixture
def ticket(ticket_dir: Path) -> TicketLifecycle:
    return TicketLifecycle(ticket_dir=str(ticket_dir))


class TestInitialState:
    def test_initial_state_is_not_started(self, ticket: TicketLifecycle) -> None:
        assert ticket.state == "not_started"

    def test_load_restores_state(self, ticket_dir: Path) -> None:
        state_file = ticket_dir / "ticket.json"
        state_file.write_text(json.dumps({"state": "scoped", "facts": {"issue_url": "http://x"}}))
        t = TicketLifecycle(ticket_dir=str(ticket_dir))
        assert t.state == "scoped"
        assert t.facts["issue_url"] == "http://x"


class TestHappyPath:
    """Walk through the full delivery lifecycle."""

    def test_scope_transitions_to_scoped(self, ticket: TicketLifecycle) -> None:
        ticket.scope(issue_url="https://gitlab.com/org/repo/-/issues/1234")
        assert ticket.state == "scoped"
        assert ticket.facts["issue_url"] == "https://gitlab.com/org/repo/-/issues/1234"

    def test_start_transitions_to_started(self, ticket: TicketLifecycle) -> None:
        ticket.scope(issue_url="https://example.com/1")
        ticket.start(worktree_dirs=["/tmp/wt1"])
        assert ticket.state == "started"

    def test_code_transitions_to_coded(self, ticket: TicketLifecycle) -> None:
        ticket.scope(issue_url="https://example.com/1")
        ticket.start(worktree_dirs=["/tmp/wt1"])
        ticket.code()
        assert ticket.state == "coded"

    def test_test_transitions_to_tested(self, ticket: TicketLifecycle) -> None:
        ticket.scope(issue_url="https://example.com/1")
        ticket.start(worktree_dirs=["/tmp/wt1"])
        ticket.code()
        ticket.test(passed=True)
        assert ticket.state == "tested"

    def test_review_transitions_to_reviewed(self, ticket: TicketLifecycle) -> None:
        ticket.scope(issue_url="https://example.com/1")
        ticket.start(worktree_dirs=["/tmp/wt1"])
        ticket.code()
        ticket.test(passed=True)
        ticket.review()
        assert ticket.state == "reviewed"

    def test_ship_transitions_to_shipped(self, ticket: TicketLifecycle) -> None:
        ticket.scope(issue_url="https://example.com/1")
        ticket.start(worktree_dirs=["/tmp/wt1"])
        ticket.code()
        ticket.test(passed=True)
        ticket.review()
        ticket.ship(mr_urls=["https://gitlab.com/org/repo/-/merge_requests/1"])
        assert ticket.state == "shipped"
        assert ticket.facts["mr_urls"] == ["https://gitlab.com/org/repo/-/merge_requests/1"]

    def test_request_review_transitions_to_in_review(self, ticket: TicketLifecycle) -> None:
        ticket.scope(issue_url="https://example.com/1")
        ticket.start(worktree_dirs=["/tmp/wt1"])
        ticket.code()
        ticket.test(passed=True)
        ticket.review()
        ticket.ship(mr_urls=["https://gitlab.com/org/repo/-/merge_requests/1"])
        ticket.request_review()
        assert ticket.state == "in_review"

    def test_merge_transitions_to_merged(self, ticket: TicketLifecycle) -> None:
        ticket.scope(issue_url="https://example.com/1")
        ticket.start(worktree_dirs=["/tmp/wt1"])
        ticket.code()
        ticket.test(passed=True)
        ticket.review()
        ticket.ship(mr_urls=["https://gitlab.com/org/repo/-/merge_requests/1"])
        ticket.request_review()
        ticket.mark_merged()
        assert ticket.state == "merged"

    def test_deliver_transitions_to_delivered(self, ticket: TicketLifecycle) -> None:
        ticket.scope(issue_url="https://example.com/1")
        ticket.start(worktree_dirs=["/tmp/wt1"])
        ticket.code()
        ticket.test(passed=True)
        ticket.review()
        ticket.ship(mr_urls=["https://gitlab.com/org/repo/-/merge_requests/1"])
        ticket.request_review()
        ticket.mark_merged()
        ticket.mark_delivered()
        assert ticket.state == "delivered"


class TestGates:
    """Verify that invalid transitions are blocked."""

    def test_cannot_start_without_scoping(self, ticket: TicketLifecycle) -> None:
        with pytest.raises(InvalidTransitionError):
            ticket.start(worktree_dirs=["/tmp/wt1"])

    def test_cannot_code_without_starting(self, ticket: TicketLifecycle) -> None:
        ticket.scope(issue_url="https://example.com/1")
        with pytest.raises(InvalidTransitionError):
            ticket.code()

    def test_cannot_test_without_coding(self, ticket: TicketLifecycle) -> None:
        ticket.scope(issue_url="https://example.com/1")
        ticket.start(worktree_dirs=["/tmp/wt1"])
        with pytest.raises(InvalidTransitionError):
            ticket.test(passed=True)

    def test_cannot_ship_without_reviewing(self, ticket: TicketLifecycle) -> None:
        ticket.scope(issue_url="https://example.com/1")
        ticket.start(worktree_dirs=["/tmp/wt1"])
        ticket.code()
        ticket.test(passed=True)
        # Skip review, try to ship
        with pytest.raises(InvalidTransitionError):
            ticket.ship(mr_urls=["x"])


class TestReworkLoops:
    """Verify backward transitions for rework."""

    def test_rework_from_coded_to_started(self, ticket: TicketLifecycle) -> None:
        ticket.scope(issue_url="https://example.com/1")
        ticket.start(worktree_dirs=["/tmp/wt1"])
        ticket.code()
        ticket.rework()
        assert ticket.state == "started"

    def test_rework_from_tested_to_started(self, ticket: TicketLifecycle) -> None:
        ticket.scope(issue_url="https://example.com/1")
        ticket.start(worktree_dirs=["/tmp/wt1"])
        ticket.code()
        ticket.test(passed=True)
        ticket.rework()
        assert ticket.state == "started"

    def test_rework_from_reviewed_to_started(self, ticket: TicketLifecycle) -> None:
        ticket.scope(issue_url="https://example.com/1")
        ticket.start(worktree_dirs=["/tmp/wt1"])
        ticket.code()
        ticket.test(passed=True)
        ticket.review()
        ticket.rework()
        assert ticket.state == "started"


class TestSaveCleanup:
    def test_save_deletes_file_when_not_started_no_facts(self, ticket_dir: Path) -> None:
        state_file = ticket_dir / "ticket.json"
        state_file.write_text(json.dumps({"state": "scoped", "facts": {}}))
        t = TicketLifecycle(ticket_dir=str(ticket_dir))
        # Force back to not_started with no facts
        t.state = "not_started"
        t.facts = {}
        t.save()
        assert not state_file.is_file()

    def test_save_noop_when_not_started_no_file(self, ticket_dir: Path) -> None:
        state_file = ticket_dir / "ticket.json"
        t = TicketLifecycle(ticket_dir=str(ticket_dir))
        t.save()
        assert not state_file.is_file()


class TestStatus:
    def test_status_returns_state_and_transitions(self, ticket: TicketLifecycle) -> None:
        s = ticket.status()
        assert s["state"] == "not_started"
        assert s["ticket_dir"] == ticket.ticket_dir
        assert "facts" in s
        assert "available_transitions" in s


class TestPersistence:
    def test_save_and_reload(self, ticket_dir: Path) -> None:
        t = TicketLifecycle(ticket_dir=str(ticket_dir))
        t.scope(issue_url="https://example.com/1")
        t.start(worktree_dirs=["/tmp/wt1"])
        t.code()

        # Reload from disk
        t2 = TicketLifecycle(ticket_dir=str(ticket_dir))
        assert t2.state == "coded"
        assert t2.facts["issue_url"] == "https://example.com/1"

    def test_available_transitions(self, ticket: TicketLifecycle) -> None:
        avail = ticket.available_transitions()
        method_names = [t["method"] for t in avail]
        assert "scope" in method_names
        assert "code" not in method_names
