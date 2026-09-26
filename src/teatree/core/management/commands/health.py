"""``t3 <overlay> health show`` — the global operational-health detail view (PR-17).

Reconciles the operational-health registry (:mod:`teatree.core.factory.operational_health`)
so the view is current, then prints the green/yellow/red verdict plus every open
:class:`~teatree.core.models.known_issue.KnownIssue` row as a table with clickable
evidence. ``add`` and ``dismiss`` are the two operator verbs — record a manual
issue the deterministic signals cannot see, or acknowledge an auto-derived one.

Read-mostly: ``show`` reconciles (upserts derived rows, auto-resolves cleared
ones) but never mutates a ticket; ``add``/``dismiss`` write exactly one row.
"""

import datetime as dt
import io
from typing import IO, Annotated, TypedDict, cast

import typer
from django.utils import timezone
from django_typer.management import command, initialize

from teatree.core.factory.operational_health import HealthReport, reconcile_health
from teatree.core.machine_output import MachineOutputCommand, emit
from teatree.core.models.known_issue import KnownIssue
from teatree.core.ref_render import render_ref
from teatree.core.table_output import print_table


class _IssueRow(TypedDict):
    id: int
    severity: str
    overlay: str
    kind: str
    summary: str
    evidence_url: str
    first_seen: str
    last_seen: str


class HealthPayload(TypedDict):
    status: str
    open_count: int
    issues: list[_IssueRow]


_AGE_UNITS: tuple[tuple[int, str], ...] = ((86_400, "d"), (3_600, "h"), (60, "m"), (1, "s"))


def _age(moment: dt.datetime, *, now: dt.datetime) -> str:
    seconds = max(int((now - moment).total_seconds()), 0)
    return next(
        (f"{seconds // size}{label} ago" for size, label in _AGE_UNITS if seconds >= size),
        "0s ago",
    )


def _render_report(report: HealthReport) -> str:
    """Render the verdict header + a table of the open issues (clickable evidence).

    The header stamp and the per-row age are load-bearing: this output gets pasted
    into reports and read hours later, and a finding with no age is indistinguishable
    from one taken seconds ago — which is how a fixed problem goes on being relayed as
    current.
    """
    now = timezone.now()
    buffer = io.StringIO()
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    buffer.write(f"health: {report.status.value} · {report.open_count} open · measured {stamp}\n")
    if not report.open_issues:
        return buffer.getvalue().rstrip("\n")
    rows = [
        [
            str(issue.pk),
            issue.severity,
            issue.overlay or "-",
            _age(issue.last_seen, now=now),
            render_ref(issue.summary, url=issue.evidence_url),
        ]
        for issue in report.open_issues
    ]
    print_table(
        ["Id", "Severity", "Overlay", "Last seen", "Issue"],
        rows,
        title="Open issues",
        stream=buffer,
    )
    return buffer.getvalue().rstrip("\n")


class Command(MachineOutputCommand):
    @initialize()
    def init(self) -> None:
        """``t3 <overlay> health`` group root."""

    @command()
    def show(
        self,
        *,
        json_output: Annotated[
            bool,
            typer.Option("--json", help="Emit the report as JSON instead of the table view."),
        ] = False,
    ) -> HealthPayload:
        """Reconcile and print the global-health verdict + open KnownIssue rows."""
        report = reconcile_health()
        payload: HealthPayload = {
            "status": report.status.value,
            "open_count": report.open_count,
            "issues": [
                {
                    "id": issue.pk,
                    "severity": issue.severity,
                    "overlay": issue.overlay,
                    "kind": issue.kind,
                    "summary": issue.summary,
                    "evidence_url": issue.evidence_url,
                    "first_seen": issue.first_seen.isoformat(),
                    "last_seen": issue.last_seen.isoformat(),
                }
                for issue in report.open_issues
            ],
        }
        self.print_result = False
        emit(
            payload,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=_render_report(report),
        )
        return payload

    @command()
    def add(
        self,
        text: Annotated[str, typer.Argument(help="The issue text to record.")],
        *,
        critical: Annotated[
            bool,
            typer.Option("--critical", help="Record at critical severity (default: warning)."),
        ] = False,
    ) -> str:
        """Record a manual operational-health issue the deterministic signals miss."""
        severity = KnownIssue.Severity.CRITICAL if critical else KnownIssue.Severity.WARNING
        issue = KnownIssue.objects.add_manual(text, severity=severity)
        return f"recorded known-issue {issue.pk} ({severity})"

    @command()
    def dismiss(
        self,
        issue_id: Annotated[int, typer.Argument(help="The KnownIssue id to dismiss.")],
    ) -> str:
        """Acknowledge and close an open issue by id."""
        if KnownIssue.objects.dismiss(issue_id):
            return f"dismissed known-issue {issue_id}"
        return f"no open known-issue {issue_id}"
