"""Scan a code-host for issues assigned to the active user that are ready to start.

When an issue carries the overlay's "ready" label and is assigned to the
user, the dispatcher creates the corresponding :class:`Ticket` and lets
the FSM ``start()`` transition take over (BLUEPRINT § 5.6).
"""

from dataclasses import dataclass, field
from typing import cast

from teatree.backends.protocols import CodeHostBackend
from teatree.core.sync import RawAPIDict
from teatree.loop.scanners.base import ScanSignal


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
    """List issues assigned to the user with the configured ready-label."""

    host: CodeHostBackend
    list_assigned: object = None  # callable(host, author) -> list[RawAPIDict]
    ready_labels: tuple[str, ...] = field(default_factory=tuple)
    name: str = "assigned_issues"

    def scan(self) -> list[ScanSignal]:
        author = self.host.current_user()
        if not author or self.list_assigned is None:
            return []
        issues = self.list_assigned(self.host, author)  # ty: ignore[call-non-callable]
        signals: list[ScanSignal] = []
        for issue in issues:
            labels = _issue_labels(issue)
            if self.ready_labels and not any(label in labels for label in self.ready_labels):
                continue
            signals.append(
                ScanSignal(
                    kind="assigned_issue.ready",
                    summary=f"Ready to start: {_issue_title(issue)}",
                    payload={"url": _issue_url(issue), "raw": issue, "labels": labels},
                )
            )
        return signals
