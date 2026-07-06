"""``t3 <overlay> health show`` — the global operational-health detail view (PR-17).

Reconciles the operational-health registry (:mod:`teatree.core.factory.operational_health`)
so the view is current, then prints the green/yellow/red verdict plus every open
:class:`~teatree.core.models.known_issue.KnownIssue` row as a table with clickable
evidence. ``add`` and ``dismiss`` are the two operator verbs — record a manual
issue the deterministic signals cannot see, or acknowledge an auto-derived one.

Read-mostly: ``show`` reconciles (upserts derived rows, auto-resolves cleared
ones) but never mutates a ticket; ``add``/``dismiss`` write exactly one row.
"""

import io
import json
from typing import Annotated

import typer
from django_typer.management import TyperCommand, command, initialize

from teatree.core.factory.operational_health import HealthReport, reconcile_health
from teatree.core.models.known_issue import KnownIssue
from teatree.core.ref_render import render_ref
from teatree.core.table_output import print_table


def _render_report(report: HealthReport) -> str:
    """Render the verdict header + a table of the open issues (clickable evidence)."""
    buffer = io.StringIO()
    buffer.write(f"health: {report.status.value} · {report.open_count} open\n")
    if not report.open_issues:
        return buffer.getvalue().rstrip("\n")
    rows = [
        [
            str(issue.pk),
            issue.severity,
            issue.overlay or "-",
            render_ref(issue.summary, url=issue.evidence_url),
        ]
        for issue in report.open_issues
    ]
    print_table(["Id", "Severity", "Overlay", "Issue"], rows, title="Open issues", stream=buffer)
    return buffer.getvalue().rstrip("\n")


class Command(TyperCommand):
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
    ) -> str:
        """Reconcile and print the global-health verdict + open KnownIssue rows."""
        report = reconcile_health()
        if json_output:
            return json.dumps(
                {
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
                        }
                        for issue in report.open_issues
                    ],
                },
            )
        return _render_report(report)

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
