"""DB-backed tests for ``AssignedIssuesScanner`` auto-start dedup and budget cap."""

from dataclasses import dataclass, field
from typing import Any

from django.test import TestCase

from teatree.core.models.config_setting import ConfigSetting
from teatree.core.models.ticket import Ticket
from teatree.loop.scanners.assigned_issues import AssignedIssuesScanner
from teatree.types import RawAPIDict


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
    def list_my_prs(self, *, author: str, updated_after: str | None = None) -> list[RawAPIDict]:
        _ = (author, updated_after)
        return []

    def list_review_requested_prs(self, *, reviewer: str, updated_after: str | None = None) -> list[RawAPIDict]:
        _ = (reviewer, updated_after)
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

    def setUp(self) -> None:
        # This suite exercises auto-start dedup + budget, orthogonal to the
        # admission policy; pin ``all`` so an unassigned/unlabeled ready issue is
        # admitted and the budget assertions stay about the budget.
        ConfigSetting.objects.set_value("admission_policy", "all")

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

    def test_notify_mode_dedupes_against_tracked_tickets(self) -> None:
        Ticket.objects.create(overlay=self.OVERLAY, issue_url=self.READY_URL_A, state=Ticket.State.STARTED)
        host = _Host(issues=[self._ready_issue(self.READY_URL_A)])
        scanner = AssignedIssuesScanner(host=host, ready_labels=("ready",), auto_start=False, overlay_name=self.OVERLAY)
        signals = scanner.scan()
        assert signals == []

    def test_payload_carries_auto_start_true_in_auto_mode(self) -> None:
        host = _Host(issues=[self._ready_issue(self.READY_URL_A)])
        signals = self._scanner(host, max_concurrent=1).scan()
        assert signals[0].payload["auto_start"] is True


class AssignedIssuesAdmissionPolicyTests(TestCase):
    """The per-overlay admission policy gates AUTONOMOUS auto-start (#3573).

    A non-auto-start (manual triage) signal is NOT gated — the policy governs
    only autonomous work.
    """

    OVERLAY = "acme"
    URL = "https://example.com/issues/200"

    def _auto_scanner(self, host: _Host) -> AssignedIssuesScanner:
        return AssignedIssuesScanner(
            host=host,
            ready_labels=("ready",),
            auto_start=True,
            max_concurrent=5,
            overlay_name=self.OVERLAY,
            identities=("alice",),
        )

    def _issue(self, *, assignee: str = "", extra_label: str = "") -> RawAPIDict:
        labels = ["ready", *([extra_label] if extra_label else [])]
        issue: RawAPIDict = {"web_url": self.URL, "title": "ready", "labels": labels}
        if assignee:
            issue["assignees"] = [{"login": assignee}]
        return issue

    def test_default_rejects_unassigned_unlabeled_auto_start(self) -> None:
        # No config row → the strict default; a ready-but-unassigned/unlabeled
        # issue is never auto-started.
        assert self._auto_scanner(_Host(issues=[self._issue()])).scan() == []

    def test_assigned_and_labeled_admits_assigned_and_labeled(self) -> None:
        ConfigSetting.objects.set_value("admission_policy", "assigned_and_labeled")
        host = _Host(issues=[self._issue(assignee="alice", extra_label="t3-auto")])
        assert [s.payload["url"] for s in self._auto_scanner(host).scan()] == [self.URL]

    def test_assigned_policy_admits_owner_assigned(self) -> None:
        ConfigSetting.objects.set_value("admission_policy", "assigned")
        host = _Host(issues=[self._issue(assignee="alice")])
        assert len(self._auto_scanner(host).scan()) == 1

    def test_manual_triage_signal_is_not_gated_by_policy(self) -> None:
        # No config row → strict default, but auto_start=False surfaces for triage.
        host = _Host(issues=[self._issue()])
        scanner = AssignedIssuesScanner(host=host, ready_labels=("ready",), auto_start=False, overlay_name=self.OVERLAY)
        assert [s.payload["url"] for s in scanner.scan()] == [self.URL]
