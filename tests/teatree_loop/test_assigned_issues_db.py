"""DB-backed tests for ``AssignedIssuesScanner`` auto-start dedup and budget cap."""

from dataclasses import dataclass, field
from typing import Any

from django.test import TestCase

from teatree.core.models.ticket import Ticket
from teatree.core.sync import RawAPIDict
from teatree.loop.scanners.assigned_issues import AssignedIssuesScanner


@dataclass
class _Host:
    """Minimal CodeHostBackend stub — only the methods the scanner calls."""

    user: str = "alice"
    issues: list[RawAPIDict] = field(default_factory=list)

    def current_user(self) -> str:
        return self.user

    def list_assigned_issues(self, *, assignee: str) -> list[RawAPIDict]:
        _ = assignee
        return self.issues

    # The scanner never calls these but the Protocol requires them.
    def list_my_prs(self, *, author: str) -> list[RawAPIDict]:
        _ = author
        return []

    def list_review_requested_prs(self, *, reviewer: str) -> list[RawAPIDict]:
        _ = reviewer
        return []

    def create_pr(self, spec: Any) -> RawAPIDict:
        _ = spec
        return {}

    def post_pr_comment(self, *, repo: str, pr_iid: int, body: str) -> RawAPIDict:
        _ = (repo, pr_iid, body)
        return {}

    def update_pr_comment(self, *, repo: str, pr_iid: int, comment_id: int, body: str) -> RawAPIDict:
        _ = (repo, pr_iid, comment_id, body)
        return {}

    def list_pr_comments(self, *, repo: str, pr_iid: int) -> list[RawAPIDict]:
        _ = (repo, pr_iid)
        return []

    def upload_file(self, *, repo: str, filepath: str) -> RawAPIDict:
        _ = (repo, filepath)
        return {}

    def get_issue(self, issue_url: str) -> RawAPIDict:
        _ = issue_url
        return {}


class AssignedIssuesAutoStartTests(TestCase):
    """Auto-start mode dedupes against active tickets and caps emissions."""

    OVERLAY = "acme"
    READY_URL_A = "https://example.com/issues/100"
    READY_URL_B = "https://example.com/issues/101"
    READY_URL_C = "https://example.com/issues/102"

    def _scanner(self, host: _Host, *, max_concurrent: int = 1) -> AssignedIssuesScanner:
        return AssignedIssuesScanner(
            host=host,
            ready_labels=("ready",),
            auto_start=True,
            max_concurrent=max_concurrent,
            overlay_name=self.OVERLAY,
        )

    def _ready_issue(self, url: str, title: str = "ready") -> RawAPIDict:
        return {"web_url": url, "title": title, "labels": ["ready"]}

    def test_skips_issues_already_tracked_by_active_tickets(self) -> None:
        Ticket.objects.create(overlay=self.OVERLAY, issue_url=self.READY_URL_A, state=Ticket.State.STARTED)
        host = _Host(issues=[self._ready_issue(self.READY_URL_A), self._ready_issue(self.READY_URL_B)])
        signals = self._scanner(host, max_concurrent=10).scan()
        assert [s.payload["url"] for s in signals] == [self.READY_URL_B]

    def test_does_not_skip_terminal_state_tickets(self) -> None:
        Ticket.objects.create(overlay=self.OVERLAY, issue_url=self.READY_URL_A, state=Ticket.State.DELIVERED)
        host = _Host(issues=[self._ready_issue(self.READY_URL_A)])
        signals = self._scanner(host, max_concurrent=10).scan()
        assert [s.payload["url"] for s in signals] == [self.READY_URL_A]

    def test_caps_signals_at_max_concurrent(self) -> None:
        host = _Host(
            issues=[
                self._ready_issue(self.READY_URL_A),
                self._ready_issue(self.READY_URL_B),
                self._ready_issue(self.READY_URL_C),
            ]
        )
        signals = self._scanner(host, max_concurrent=2).scan()
        assert [s.payload["url"] for s in signals] == [self.READY_URL_A, self.READY_URL_B]

    def test_subtracts_in_flight_auto_starts_from_budget(self) -> None:
        Ticket.objects.create(
            overlay=self.OVERLAY,
            issue_url="https://example.com/issues/old",
            state=Ticket.State.CODED,
            extra={"auto_started": True},
        )
        host = _Host(issues=[self._ready_issue(self.READY_URL_A), self._ready_issue(self.READY_URL_B)])
        # max=2, in-flight=1 → budget 1 → only first new signal emitted
        signals = self._scanner(host, max_concurrent=2).scan()
        assert [s.payload["url"] for s in signals] == [self.READY_URL_A]

    def test_in_review_tickets_do_not_count_toward_budget(self) -> None:
        Ticket.objects.create(
            overlay=self.OVERLAY,
            issue_url="https://example.com/issues/old",
            state=Ticket.State.IN_REVIEW,
            extra={"auto_started": True},
        )
        host = _Host(issues=[self._ready_issue(self.READY_URL_A)])
        signals = self._scanner(host, max_concurrent=1).scan()
        assert [s.payload["url"] for s in signals] == [self.READY_URL_A]

    def test_other_overlay_tickets_do_not_consume_budget(self) -> None:
        Ticket.objects.create(
            overlay="other-overlay",
            issue_url="https://example.com/issues/other",
            state=Ticket.State.STARTED,
            extra={"auto_started": True},
        )
        host = _Host(issues=[self._ready_issue(self.READY_URL_A)])
        signals = self._scanner(host, max_concurrent=1).scan()
        assert [s.payload["url"] for s in signals] == [self.READY_URL_A]

    def test_notify_mode_skips_db_lookup_and_dedup(self) -> None:
        Ticket.objects.create(overlay=self.OVERLAY, issue_url=self.READY_URL_A, state=Ticket.State.STARTED)
        host = _Host(issues=[self._ready_issue(self.READY_URL_A)])
        scanner = AssignedIssuesScanner(host=host, ready_labels=("ready",), auto_start=False, overlay_name=self.OVERLAY)
        signals = scanner.scan()
        assert [s.payload["url"] for s in signals] == [self.READY_URL_A]
        assert signals[0].payload["auto_start"] is False

    def test_payload_carries_auto_start_true_in_auto_mode(self) -> None:
        host = _Host(issues=[self._ready_issue(self.READY_URL_A)])
        signals = self._scanner(host, max_concurrent=1).scan()
        assert signals[0].payload["auto_start"] is True
