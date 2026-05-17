"""``t3 <overlay> standup`` — read-only auto-generated daily update (issue #563).

Thin wrapper over :func:`teatree.core.standup.generate_standup`. Parses the
time window and returns typed, JSON-serializable structures — the return
value is the output channel, mirroring the ``followup``/``ticket``/``tasks``
commands (``django-typer`` serializes the return for the CLI). No state
mutation: every query underneath is read-only.
"""

import os
from datetime import datetime, timedelta
from typing import Annotated, TypedDict, cast

import typer
from django.utils import timezone
from django_typer.management import TyperCommand, command

from teatree.core.standup import StandupReportDict, generate_standup
from teatree.loop.scanners.stale_tickets import StaleTicketsScanner


class StaleTicketRow(TypedDict):
    ticket_id: int
    ticket_number: str
    ticket_state: str
    age_days: int
    summary: str


class Command(TyperCommand):
    @command()
    def generate(
        self,
        *,
        days: Annotated[int, typer.Option(help="Window size in days (default: last business day).")] = 1,
        since: Annotated[str, typer.Option(help="ISO timestamp override for the window start.")] = "",
    ) -> StandupReportDict:
        """Generate the standup from existing transition + attempt data (read-only).

        Returns ``{since, yesterday, blockers, markdown}`` — ``markdown`` is
        the pre-rendered human view alongside the structured rows.
        """
        window_start = self._resolve_since(days=days, since=since)
        report = generate_standup(
            since=window_start,
            overlay_name=os.environ.get("T3_OVERLAY_NAME", ""),
        )
        return report.to_dict()

    @command()
    def stale(
        self,
        *,
        days: Annotated[int, typer.Option(help="Inactivity threshold in days.")] = 3,
    ) -> list[StaleTicketRow]:
        """List tickets with no activity past the threshold (read-only)."""
        scanner = StaleTicketsScanner(
            overlay_name=os.environ.get("T3_OVERLAY_NAME", ""),
            threshold_days=days,
        )
        return [
            StaleTicketRow(
                ticket_id=cast("int", signal.payload["ticket_id"]),
                ticket_number=cast("str", signal.payload["ticket_number"]),
                ticket_state=cast("str", signal.payload["ticket_state"]),
                age_days=cast("int", signal.payload["age_days"]),
                summary=signal.summary,
            )
            for signal in scanner.scan()
        ]

    @staticmethod
    def _resolve_since(*, days: int, since: str) -> datetime:
        if since:
            parsed = datetime.fromisoformat(since)
            if timezone.is_naive(parsed):
                parsed = timezone.make_aware(parsed)
            return parsed
        return timezone.now() - timedelta(days=days)
