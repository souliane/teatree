"""DB-backed tests for ``TicketCompletionScanner`` and its dispatch integration."""

from dataclasses import dataclass, field
from typing import Any

from django.test import TestCase

from teatree.core.models.ticket import Ticket
from teatree.core.overlay import OverlayBase
from teatree.core.sync import RawAPIDict
from teatree.loop.dispatch import dispatch
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.ticket_completion import TicketCompletionScanner


@dataclass
class _Host:
    issues_by_url: dict[str, RawAPIDict] = field(default_factory=dict)
    get_issue_calls: list[str] = field(default_factory=list)

    def current_user(self) -> str:
        return "alice"

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


class _AcmeOverlay(OverlayBase):
    def get_repos(self) -> list[str]:
        return ["acme-repo"]

    def get_provision_steps(self, worktree: Any) -> list:
        return []


class _GitLabOverlay(OverlayBase):
    """Overlay that considers an issue done when it has the DEV Review label."""

    def get_repos(self) -> list[str]:
        return ["gitlab-repo"]

    def get_provision_steps(self, worktree: Any) -> list:
        return []

    def is_issue_done(self, issue_data: dict[str, object]) -> bool:
        labels = issue_data.get("labels")
        if not isinstance(labels, list):
            return False
        label_names = []
        for item in labels:
            if isinstance(item, str):
                label_names.append(item)
            elif isinstance(item, dict):
                name = item.get("name")
                if isinstance(name, str):
                    label_names.append(name)
        return "Process:DEV Review" in label_names


class TicketCompletionScannerTests(TestCase):
    OVERLAY = "t3-acme"
    URL = "https://example.com/issues/100"

    def _scanner(self, host: _Host, overlay: OverlayBase | None = None) -> TicketCompletionScanner:
        return TicketCompletionScanner(
            host=host,
            overlay=overlay or _AcmeOverlay(),
            overlay_name=self.OVERLAY,
        )

    def _ticket(self, *, state: str = Ticket.State.SHIPPED, url: str | None = None) -> Ticket:
        return Ticket.objects.create(overlay=self.OVERLAY, issue_url=url or self.URL, state=state)

    def test_detects_closed_github_issue(self) -> None:
        self._ticket(state=Ticket.State.SHIPPED)
        host = _Host(issues_by_url={self.URL: {"state": "closed"}})
        signals = self._scanner(host).scan()
        assert len(signals) == 1
        assert signals[0].kind == "ticket.completion_detected"

    def test_detects_completed_github_issue(self) -> None:
        self._ticket(state=Ticket.State.IN_REVIEW)
        host = _Host(issues_by_url={self.URL: {"state": "completed"}})
        signals = self._scanner(host).scan()
        assert len(signals) == 1

    def test_no_signal_for_open_issue(self) -> None:
        self._ticket(state=Ticket.State.SHIPPED)
        host = _Host(issues_by_url={self.URL: {"state": "open"}})
        assert self._scanner(host).scan() == []

    def test_only_scans_post_ship_states(self) -> None:
        for state in (
            Ticket.State.NOT_STARTED,
            Ticket.State.STARTED,
            Ticket.State.CODED,
            Ticket.State.TESTED,
            Ticket.State.REVIEWED,
            Ticket.State.DELIVERED,
        ):
            Ticket.objects.create(overlay=self.OVERLAY, issue_url=f"{self.URL}/{state}", state=state)
        host = _Host()
        assert self._scanner(host).scan() == []
        assert host.get_issue_calls == []

    def test_scans_shipped_in_review_merged(self) -> None:
        for state in (Ticket.State.SHIPPED, Ticket.State.IN_REVIEW, Ticket.State.MERGED):
            Ticket.objects.create(
                overlay=self.OVERLAY,
                issue_url=f"{self.URL}/{state}",
                state=state,
            )
        host = _Host(
            issues_by_url={
                f"{self.URL}/{Ticket.State.SHIPPED}": {"state": "closed"},
                f"{self.URL}/{Ticket.State.IN_REVIEW}": {"state": "closed"},
                f"{self.URL}/{Ticket.State.MERGED}": {"state": "closed"},
            }
        )
        signals = self._scanner(host).scan()
        assert len(signals) == 3

    def test_skips_empty_url(self) -> None:
        Ticket.objects.create(overlay=self.OVERLAY, issue_url="", state=Ticket.State.SHIPPED)
        host = _Host()
        assert self._scanner(host).scan() == []

    def test_handles_error_response(self) -> None:
        self._ticket(state=Ticket.State.SHIPPED)
        host = _Host(issues_by_url={})
        assert self._scanner(host).scan() == []

    def test_gitlab_overlay_label_based_done(self) -> None:
        self._ticket(state=Ticket.State.IN_REVIEW)
        host = _Host(
            issues_by_url={
                self.URL: {
                    "state": "opened",
                    "labels": [{"name": "Process:DEV Review"}],
                }
            }
        )
        signals = self._scanner(host, overlay=_GitLabOverlay()).scan()
        assert len(signals) == 1

    def test_gitlab_overlay_not_done_without_label(self) -> None:
        self._ticket(state=Ticket.State.IN_REVIEW)
        host = _Host(
            issues_by_url={
                self.URL: {
                    "state": "opened",
                    "labels": [{"name": "Process:Development"}],
                }
            }
        )
        assert self._scanner(host, overlay=_GitLabOverlay()).scan() == []

    def test_filters_by_overlay_name(self) -> None:
        Ticket.objects.create(overlay="other-overlay", issue_url=self.URL, state=Ticket.State.SHIPPED)
        host = _Host(issues_by_url={self.URL: {"state": "closed"}})
        assert self._scanner(host).scan() == []

    def test_payload_contains_ticket_info(self) -> None:
        ticket = self._ticket(state=Ticket.State.MERGED)
        host = _Host(issues_by_url={self.URL: {"state": "closed"}})
        signals = self._scanner(host).scan()
        assert signals[0].payload["ticket_id"] == ticket.pk
        assert signals[0].payload["ticket_state"] == "merged"
        assert signals[0].payload["issue_url"] == self.URL


class DispatchCompletionTests(TestCase):
    def test_completion_signal_dispatches_to_mechanical(self) -> None:
        signal = ScanSignal(
            kind="ticket.completion_detected",
            summary="Ticket 100 — issue done",
            payload={"ticket_id": 1, "ticket_state": "shipped"},
        )
        actions = dispatch([signal])
        assert len(actions) == 1
        assert actions[0].kind == "mechanical"
        assert actions[0].zone == "ticket_completion"

    def test_completion_payload_propagated(self) -> None:
        payload: dict[str, object] = {"ticket_id": 42, "ticket_state": "in_review", "issue_url": "http://x"}
        actions = dispatch([ScanSignal(kind="ticket.completion_detected", summary="x", payload=payload)])
        assert actions[0].payload == payload
