"""Per-mini-loop ``_build_jobs`` coverage.

Each mini-loop's ``_build_jobs`` callable is exercised with a stub
backend list so the delegation paths into ``teatree.loop.tick_jobs`` are
walked. The tests assert structural shape (jobs are a list of
``_ScannerJob`` records); the scanner classes themselves are covered
by the existing ``tests/teatree_loop/`` suite.
"""

from typing import Any
from unittest.mock import MagicMock

import pytest

from teatree.loops.arch_review.loop import MINI_LOOP as ARCH_REVIEW_LOOP
from teatree.loops.audit.loop import MINI_LOOP as AUDIT_LOOP
from teatree.loops.dispatch.loop import MINI_LOOP as DISPATCH_LOOP
from teatree.loops.dogfood.loop import MINI_LOOP as DOGFOOD_LOOP
from teatree.loops.followup.loop import MINI_LOOP as FOLLOWUP_LOOP
from teatree.loops.housekeeping.loop import MINI_LOOP as HOUSEKEEPING_LOOP
from teatree.loops.inbox.loop import MINI_LOOP as INBOX_LOOP
from teatree.loops.news.loop import MINI_LOOP as NEWS_LOOP
from teatree.loops.review.loop import MINI_LOOP as REVIEW_LOOP
from teatree.loops.ship.loop import MINI_LOOP as SHIP_LOOP
from teatree.loops.tickets.loop import MINI_LOOP as TICKETS_LOOP


@pytest.fixture
def stub_backend() -> Any:
    """A backend stub matching the shape ``teatree.loop.tick_jobs`` expects."""
    backend = MagicMock()
    backend.name = "stub-overlay"
    backend.hosts = ()  # no hosts → most per-host scanners skip
    backend.identities = ("alice",)
    backend.ready_labels = ()
    backend.exclude_labels = ()
    backend.auto_start_assigned_issues = False
    backend.max_concurrent_auto_starts = 1
    backend.stale_threshold_days = 3
    backend.external_db = None
    backend.overlay = None
    backend.messaging = None
    return backend


@pytest.fixture
def stub_messaging() -> Any:
    messaging = MagicMock()
    messaging.fetch_mentions = MagicMock(return_value=[])
    messaging.fetch_dms = MagicMock(return_value=[])
    messaging.fetch_reactions = MagicMock(return_value=[])
    return messaging


class TestDispatchLoopBuildJobs:
    def test_returns_three_global_jobs(self) -> None:
        jobs = DISPATCH_LOOP.build_jobs()
        assert len(jobs) == 3
        names = {j.scanner.name for j in jobs}
        assert names == {"pending_tasks", "incoming_events", "outbound_audit"}


class TestArchReviewLoopBuildJobs:
    def test_returns_empty_when_no_backends(self) -> None:
        assert ARCH_REVIEW_LOOP.build_jobs(backends=None) == []

    def test_walks_backends_when_provided(self, stub_backend: Any) -> None:
        jobs = ARCH_REVIEW_LOOP.build_jobs(backends=[stub_backend])
        assert isinstance(jobs, list)


class TestAuditLoopBuildJobs:
    def test_returns_empty_when_no_backends(self) -> None:
        assert AUDIT_LOOP.build_jobs(backends=None) == []

    def test_walks_backends(self, stub_backend: Any) -> None:
        jobs = AUDIT_LOOP.build_jobs(backends=[stub_backend])
        assert isinstance(jobs, list)


class TestDogfoodLoopBuildJobs:
    def test_resolves_scanner_or_empty(self) -> None:
        jobs = DOGFOOD_LOOP.build_jobs()
        assert isinstance(jobs, list)


class TestNewsLoopBuildJobs:
    def test_resolves_scanner_or_empty(self) -> None:
        jobs = NEWS_LOOP.build_jobs()
        assert isinstance(jobs, list)


class TestHousekeepingLoopBuildJobs:
    def test_runs_with_no_backends(self) -> None:
        jobs = HOUSEKEEPING_LOOP.build_jobs()
        assert isinstance(jobs, list)

    def test_runs_with_backends(self, stub_backend: Any) -> None:
        jobs = HOUSEKEEPING_LOOP.build_jobs(backends=[stub_backend])
        assert isinstance(jobs, list)


class TestInboxLoopBuildJobs:
    def test_returns_empty_with_no_inputs(self) -> None:
        assert INBOX_LOOP.build_jobs() == []

    def test_single_overlay_messaging_path(self, stub_messaging: Any) -> None:
        jobs = INBOX_LOOP.build_jobs(messaging=stub_messaging)
        # mentions, dms, review_intent, red_card
        assert len(jobs) == 4
        names = {j.scanner.name for j in jobs}
        assert "slack_mentions" in names
        assert "red_card" in names

    def test_with_notion_client_only(self) -> None:
        notion = MagicMock()
        jobs = INBOX_LOOP.build_jobs(notion_client=notion)
        names = {j.scanner.name for j in jobs}
        assert "notion_view" in names

    def test_backends_branch_skipped_when_messaging_none(self, stub_backend: Any) -> None:
        jobs = INBOX_LOOP.build_jobs(backends=[stub_backend])
        assert isinstance(jobs, list)


class TestFollowupLoopBuildJobs:
    def test_returns_empty_with_no_inputs(self) -> None:
        assert FOLLOWUP_LOOP.build_jobs() == []

    def test_host_only_path(self) -> None:
        host = MagicMock()
        jobs = FOLLOWUP_LOOP.build_jobs(host=host, ready_labels=("ready",))
        assert len(jobs) == 1
        assert jobs[0].scanner.name == "assigned_issues"

    def test_backends_path(self, stub_backend: Any) -> None:
        # No hosts on the stub → no AssignedIssuesScanner jobs.
        # messaging None → no ReviewNagScanner job.
        jobs = FOLLOWUP_LOOP.build_jobs(backends=[stub_backend])
        assert jobs == []


class TestShipLoopBuildJobs:
    def test_returns_empty_with_no_inputs(self) -> None:
        assert SHIP_LOOP.build_jobs() == []

    def test_host_only_path(self) -> None:
        host = MagicMock()
        jobs = SHIP_LOOP.build_jobs(host=host)
        assert len(jobs) == 1
        assert jobs[0].scanner.name == "my_prs"

    def test_backends_path(self, stub_backend: Any) -> None:
        jobs = SHIP_LOOP.build_jobs(backends=[stub_backend])
        assert jobs == []


class TestReviewLoopBuildJobs:
    def test_returns_empty_with_no_inputs(self) -> None:
        assert REVIEW_LOOP.build_jobs() == []

    def test_host_only_path(self) -> None:
        host = MagicMock()
        jobs = REVIEW_LOOP.build_jobs(host=host)
        assert len(jobs) == 1
        assert jobs[0].scanner.name == "reviewer_prs"

    def test_backends_path(self, stub_backend: Any) -> None:
        jobs = REVIEW_LOOP.build_jobs(backends=[stub_backend])
        assert jobs == []


class TestTicketsLoopBuildJobs:
    def test_returns_empty_with_no_backends(self) -> None:
        assert TICKETS_LOOP.build_jobs() == []

    def test_walks_backend_per_overlay(self, stub_backend: Any) -> None:
        jobs = TICKETS_LOOP.build_jobs(backends=[stub_backend])
        assert len(jobs) == 2  # active + stale
        names = {j.scanner.name for j in jobs}
        assert names == {"active_tickets", "stale_tickets"}
