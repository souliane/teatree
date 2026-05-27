"""``manage.py news`` — operate on the news-scan ask-gate queue (#1391).

Three subcommands act on the durable
:class:`teatree.core.models.PendingArticleSuggestion` queue populated by
the scanning-news skill instead of direct ``gh issue create`` calls:

* ``news pending`` — list undecided suggestions, oldest first.
* ``news approve <id>`` — file the GitHub issue and stamp the row
    APPROVED with the new issue URL.
* ``news reject <id> [--reason ...]`` — drop the candidate (no issue
    is created) and stamp the row REJECTED.

The user pilots the backlog: nothing in this flow auto-creates a
ticket without a deliberate ``news approve`` call.
"""

from typing import Annotated

import typer
from django_typer.management import TyperCommand, command, initialize

from teatree.core.article_ingestion_gate import APPROVED_ISSUE_LABEL, APPROVED_ISSUE_REPO, approve_and_create_ticket
from teatree.core.models import PendingArticleSuggestion


def _format_row(row: PendingArticleSuggestion) -> str:
    age = row.created_at.isoformat() if row.created_at is not None else "?"
    title = row.title or row.url
    lines = [
        f"  #{row.pk} [{row.decision}] {age}",
        f"     {title}",
        f"     {row.url}",
    ]
    if row.summary:
        lines.append(f"     {row.summary}")
    if row.source:
        lines.append(f"     source: {row.source}")
    if row.created_ticket_url:
        lines.append(f"     ticket: {row.created_ticket_url}")
    return "\n".join(lines)


class Command(TyperCommand):
    @initialize()
    def init(self) -> None:
        """``t3 manage news`` group root."""

    @command(name="pending")
    def list_pending(
        self,
        *,
        all_rows: Annotated[
            bool,
            typer.Option("--all/--pending", help="Include approved/rejected rows."),
        ] = False,
    ) -> str:
        """List news-scan suggestions awaiting user decision."""
        if all_rows:
            rows = list(PendingArticleSuggestion.objects.order_by("-created_at"))
        else:
            rows = list(PendingArticleSuggestion.pending())
        if not rows:
            return "no pending article suggestions."
        lines = [f"{len(rows)} article suggestion(s):"]
        lines.extend(_format_row(row) for row in rows)
        return "\n".join(lines)

    @command()
    def approve(
        self,
        suggestion_id: int,
        decider_id: Annotated[
            str,
            typer.Option("--decider", help="Identity of the approver (audit trail)."),
        ] = "",
        repo: Annotated[
            str,
            typer.Option("--repo", help="GitHub repo for the new issue."),
        ] = APPROVED_ISSUE_REPO,
        label: Annotated[
            str,
            typer.Option("--label", help="Issue label."),
        ] = APPROVED_ISSUE_LABEL,
    ) -> str:
        """Approve a pending suggestion — files the GitHub issue."""
        row = approve_and_create_ticket(
            suggestion_id,
            decider_id=decider_id,
            repo=repo,
            label=label,
        )
        if row is None:
            self.stderr.write(
                f"suggestion #{suggestion_id} not found or already decided",
            )
            raise SystemExit(1)
        if row.created_ticket_url:
            return f"approved #{row.pk} — issue: {row.created_ticket_url}"
        return f"approved #{row.pk} (ticket URL not captured from gh output)"

    @command()
    def reject(
        self,
        suggestion_id: int,
        reason: Annotated[
            str,
            typer.Option("--reason", help="Why the candidate is dropped (audit trail)."),
        ] = "not relevant",
        decider_id: Annotated[
            str,
            typer.Option("--decider", help="Identity of the rejecter (audit trail)."),
        ] = "",
    ) -> str:
        """Reject a pending suggestion — no issue is created."""
        row = PendingArticleSuggestion.reject(
            suggestion_id,
            decider_id=decider_id,
            reason=reason.strip() or "not relevant",
        )
        if row is None:
            self.stderr.write(
                f"suggestion #{suggestion_id} not found or already decided",
            )
            raise SystemExit(1)
        return f"rejected #{row.pk}."
