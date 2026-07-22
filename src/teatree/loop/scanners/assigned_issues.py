"""Scan a code-host for issues assigned to the active user that are ready to start.

When an issue carries the overlay's "ready" label and is assigned to the
user, the dispatcher creates the corresponding :class:`Ticket` and lets
the FSM ``start()`` transition take over (BLUEPRINT § 5.6).
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from django.apps import apps

from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.intake.admission_policy import admit_issue
from teatree.core.intake.label_admission import intake_admits
from teatree.loop.scanners.base import ScanSignal
from teatree.types import RawAPIDict

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket

# A ticket no longer occupies the auto-start budget once it reaches IN_REVIEW
# (PR is open, awaiting review/CLEAR before the keystone merge). Earlier states
# still count: NOT_STARTED, SCOPED, STARTED, CODED, TESTED, REVIEWED, SHIPPED.
_AUTO_START_BUDGET_STATES: frozenset[str] = frozenset(
    {"not_started", "scoped", "started", "coded", "tested", "reviewed", "shipped"}
)
# A ticket in any of these states "owns" its issue URL — the scanner skips
# emitting another signal until the ticket reaches a terminal state.
_ACTIVE_TICKET_STATES: frozenset[str] = frozenset(
    {
        "not_started",
        "scoped",
        "started",
        "coded",
        "tested",
        "reviewed",
        "shipped",
        "in_review",
        "merged",
        "retrospected",
    }
)


def _issue_url(issue: RawAPIDict) -> str:
    for name in ("web_url", "html_url"):
        value = issue.get(name)
        if isinstance(value, str):
            return value
    return ""


def _issue_title(issue: RawAPIDict) -> str:
    title = issue.get("title")
    return title if isinstance(title, str) else ""


def _issue_labels(issue: RawAPIDict) -> list[str]:
    labels = issue.get("labels")
    if not isinstance(labels, list):
        return []
    out: list[str] = []
    for item in labels:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            name = cast("RawAPIDict", item).get("name")
            if isinstance(name, str):
                out.append(name)
    return out


@dataclass(slots=True)
class AssignedIssuesScanner:
    """List issues assigned to the user with the configured ready-label.

    When *auto_start* is ``True`` the scanner also dedupes against existing
    tickets and caps emissions at *max_concurrent* in-flight auto-starts; the
    dispatcher routes the remaining signals to the orchestrator agent. When
    ``False``, every ready issue surfaces in the statusline ``action_needed``
    zone for the operator to triage manually.

    ``identities`` opts the scanner into a multi-alias union query so a user
    with more than one identity on the same forge sees issues assigned to
    any of them. Empty falls back to ``host.current_user()`` (#976).
    """

    host: CodeHostBackend
    ready_labels: tuple[str, ...] = field(default_factory=tuple)
    exclude_labels: tuple[str, ...] = field(default_factory=tuple)
    auto_start: bool = False
    max_concurrent: int = 1
    overlay_name: str = ""
    identities: tuple[str, ...] = field(default_factory=tuple)
    name: str = "assigned_issues"

    def scan(self) -> list[ScanSignal]:
        assignees = self._resolve_identities()
        if not assignees:
            return []
        issues = self._collect_unique_issues(assignees)

        try:
            tracked, in_flight = self._tracked_urls_and_in_flight()
        except Exception:  # noqa: BLE001 — an in-flight-count failure degrades to zero, never breaks the scan
            tracked, in_flight = frozenset[str](), 0
        budget = max(0, self.max_concurrent - in_flight) if self.auto_start else -1

        signals: list[ScanSignal] = []
        for issue in issues:
            labels = _issue_labels(issue)
            if not intake_admits(labels, self.ready_labels, self.exclude_labels):
                continue
            url = _issue_url(issue)
            if url and url in tracked:
                continue
            # The per-overlay admission policy gates AUTONOMOUS work only: a
            # non-auto-start signal still surfaces for manual triage. Under
            # auto_start it must run BEFORE a budget slot is spent, so a rejected
            # issue never blocks an admissible one.
            if self.auto_start and not admit_issue(issue, overlay=self.overlay_name, owner_handles=assignees):
                continue
            if self.auto_start and budget == 0:
                break
            signals.append(
                ScanSignal(
                    kind="assigned_issue.ready",
                    summary=f"Ready to start: {_issue_title(issue)}",
                    payload={
                        "url": url,
                        "raw": issue,
                        "labels": labels,
                        "auto_start": self.auto_start,
                        "overlay": self.overlay_name,
                    },
                )
            )
            if self.auto_start:
                budget -= 1
        return signals

    def _tracked_urls_and_in_flight(self) -> tuple[frozenset[str], int]:
        """Return (URLs already owned by an active ticket, count of in-flight auto-starts).

        "Active" = state in :data:`_ACTIVE_TICKET_STATES`. "In-flight auto-start"
        = ``extra.auto_started is True`` and state in
        :data:`_AUTO_START_BUDGET_STATES`. Filtered by *overlay_name* when set so
        multi-overlay setups account for budget separately.
        """
        ticket_model = cast("type[Ticket]", apps.get_model("core", "Ticket"))
        qs = ticket_model.objects.filter(state__in=_ACTIVE_TICKET_STATES)
        if self.overlay_name:
            qs = qs.filter(overlay=self.overlay_name)
        tracked: set[str] = set()
        in_flight = 0
        for ticket in qs.only("issue_url", "state", "extra"):
            if ticket.issue_url:
                tracked.add(ticket.issue_url)
            extra = ticket.extra or {}
            if extra.get("auto_started") is True and ticket.state in _AUTO_START_BUDGET_STATES:
                in_flight += 1
        return frozenset(tracked), in_flight

    def _resolve_identities(self) -> tuple[str, ...]:
        if self.identities:
            return tuple(dict.fromkeys(self.identities))
        user = self.host.current_user()
        return (user,) if user else ()

    def _collect_unique_issues(self, assignees: tuple[str, ...]) -> list[RawAPIDict]:
        """Union assigned issues across *assignees*, deduped by URL."""
        seen_urls: set[str] = set()
        issues: list[RawAPIDict] = []
        for assignee in assignees:
            for issue in self.host.list_assigned_issues(assignee=assignee):
                url = _issue_url(issue)
                if url and url in seen_urls:
                    continue
                if url:
                    seen_urls.add(url)
                issues.append(issue)
        return issues
