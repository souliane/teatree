"""DB-backed tests for ``TicketDispositionScanner``."""

from dataclasses import dataclass, field
from typing import Any

from django.test import TestCase

from teatree.core.models.ticket import Ticket
from teatree.loop.scanners.ticket_dispositions import TicketDispositionScanner
from teatree.types import RawAPIDict


@dataclass
class _Host:
    """CodeHostBackend stub keyed on ``issue_url`` for ``get_issue`` lookups."""

    user: str = "alice"
    issues_by_url: dict[str, RawAPIDict] = field(default_factory=dict)
    get_issue_calls: list[str] = field(default_factory=list)

    def current_user(self) -> str:
        return self.user

    def list_my_prs(self, *, author: str, updated_after: str | None = None) -> list[RawAPIDict]:
        _ = (author, updated_after)
        return []

    def list_review_requested_prs(self, *, reviewer: str, updated_after: str | None = None) -> list[RawAPIDict]:
        _ = (reviewer, updated_after)
        return []

    def list_assigned_issues(self, *, assignee: str) -> list[RawAPIDict]:
        _ = assignee
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
        self.get_issue_calls.append(issue_url)
        return self.issues_by_url.get(issue_url, {"error": "not found"})


class TicketDispositionScannerTests(TestCase):
    OVERLAY = "acme"
    URL = "https://example.com/issues/200"

    def _scanner(self, host: _Host, *, ready_labels: tuple[str, ...] = ("ready",)) -> TicketDispositionScanner:
        return TicketDispositionScanner(host=host, ready_labels=ready_labels, overlay_name=self.OVERLAY)

    def _open_ready_issue(self) -> RawAPIDict:
        return {
            "state": "opened",
            "assignees": [{"username": "alice"}],
            "labels": [{"name": "ready"}],
        }

    def _ticket(self, *, state: str = Ticket.State.STARTED, url: str | None = None) -> Ticket:
        return Ticket.objects.create(overlay=self.OVERLAY, issue_url=url or self.URL, state=state)

    def test_no_signals_when_issue_unchanged(self) -> None:
        self._ticket()
        host = _Host(issues_by_url={self.URL: self._open_ready_issue()})
        assert self._scanner(host).scan() == []

    def test_flags_closed_issue(self) -> None:
        self._ticket()
        host = _Host(issues_by_url={self.URL: {**self._open_ready_issue(), "state": "closed"}})
        signals = self._scanner(host).scan()
        assert [s.payload["reason"] for s in signals] == ["issue_closed"]
        assert signals[0].payload["issue_url"] == self.URL

    def test_flags_unassigned_issue(self) -> None:
        self._ticket()
        host = _Host(
            issues_by_url={self.URL: {**self._open_ready_issue(), "assignees": [{"username": "bob"}]}},
        )
        signals = self._scanner(host).scan()
        assert [s.payload["reason"] for s in signals] == ["unassigned"]

    def test_flags_label_removed(self) -> None:
        self._ticket()
        host = _Host(issues_by_url={self.URL: {**self._open_ready_issue(), "labels": [{"name": "blocked"}]}})
        signals = self._scanner(host).scan()
        assert [s.payload["reason"] for s in signals] == ["label_removed"]

    def test_emits_one_signal_per_reason_when_multiple_apply(self) -> None:
        self._ticket()
        host = _Host(
            issues_by_url={
                self.URL: {
                    "state": "closed",
                    "assignees": [{"username": "bob"}],
                    "labels": [{"name": "blocked"}],
                }
            },
        )
        signals = self._scanner(host).scan()
        reasons = sorted(s.payload["reason"] for s in signals)
        assert reasons == ["issue_closed", "label_removed", "unassigned"]

    def test_skips_tickets_in_post_pr_states(self) -> None:
        for state in (Ticket.State.SHIPPED, Ticket.State.IN_REVIEW, Ticket.State.MERGED, Ticket.State.DELIVERED):
            Ticket.objects.create(overlay=self.OVERLAY, issue_url=f"{self.URL}/{state}", state=state)
        host = _Host()  # never queried — tickets filtered out before host call
        assert self._scanner(host).scan() == []
        assert host.get_issue_calls == []

    def test_skips_tickets_with_empty_url(self) -> None:
        Ticket.objects.create(overlay=self.OVERLAY, issue_url="", state=Ticket.State.STARTED)
        host = _Host()
        assert self._scanner(host).scan() == []
        assert host.get_issue_calls == []

    def test_handles_issue_lookup_error_silently(self) -> None:
        self._ticket()
        host = _Host(issues_by_url={})  # get_issue returns error
        assert self._scanner(host).scan() == []

    def test_does_not_flag_unassigned_when_assignees_empty(self) -> None:
        """An empty assignees list means 'no one' — different from 'reassigned'."""
        self._ticket()
        host = _Host(issues_by_url={self.URL: {**self._open_ready_issue(), "assignees": []}})
        signals = self._scanner(host).scan()
        assert [s.payload["reason"] for s in signals] == []

    def test_skips_label_check_when_ready_labels_empty(self) -> None:
        self._ticket()
        host = _Host(issues_by_url={self.URL: {**self._open_ready_issue(), "labels": []}})
        signals = self._scanner(host, ready_labels=()).scan()
        assert signals == []

    def test_filters_by_overlay_name(self) -> None:
        Ticket.objects.create(overlay="other", issue_url=self.URL, state=Ticket.State.STARTED)
        host = _Host(issues_by_url={self.URL: {**self._open_ready_issue(), "state": "closed"}})
        assert self._scanner(host).scan() == []

    def test_supports_github_login_field(self) -> None:
        """GitHub uses 'login', GitLab uses 'username' — both should work."""
        self._ticket()
        host = _Host(
            issues_by_url={self.URL: {**self._open_ready_issue(), "assignees": [{"login": "alice"}]}},
        )
        assert self._scanner(host).scan() == []  # alice IS assigned, no flag
