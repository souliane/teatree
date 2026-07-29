"""``manage.py ticket_short_describe`` — the CLI over the short-description writer (#1156).

Two invocation forms: ``--ticket-id <N>`` describes one ticket; ``--all-missing``
backfills every ticket with a non-blank ``extra["issue_title"]`` and a blank
``short_description`` (a one-shot sweep after rollout, before the loop has
scanned each ticket). The generation + persistence itself lives in
:mod:`teatree.core.ticket_short_description`, shared with the ``short_describe``
deterministic phase, so the CLI and the phase can never drift.
"""

from collections.abc import Callable
from typing import Annotated

import typer
from django.core.management.base import BaseCommand
from django_typer.management import TyperCommand, command

from teatree.agents.ticket_short_description import (
    TicketNotFoundError,
    describe_ticket_short_description,
    generate_short_description,
)


def describe_ticket(ticket_id: int, *, stdout_write: Callable[[str], object]) -> None:
    try:
        summary = describe_ticket_short_description(ticket_id)
    except TicketNotFoundError:
        stdout_write(f"NOOP  no ticket with id={ticket_id}")
        raise SystemExit(1) from None
    if not summary:
        stdout_write(f"NOOP  ticket {ticket_id} has no extra['issue_title'] — skipped")
        return
    stdout_write(f"OK    ticket {ticket_id}: short_description={summary!r}")


def _describe_all_missing(*, stdout_write: Callable[[str], object]) -> None:
    from teatree.core.models import Ticket  # noqa: PLC0415 — deferred: ORM import needs the app registry

    qs = Ticket.objects.filter(short_description="").exclude(extra__issue_title="")
    count = 0
    for ticket in qs:
        extra = ticket.extra if isinstance(ticket.extra, dict) else {}
        title = extra.get("issue_title", "") if isinstance(extra, dict) else ""
        title = title if isinstance(title, str) else ""
        if not title:
            continue
        summary = generate_short_description(title)
        Ticket.objects.filter(pk=ticket.pk).update(short_description=summary)
        stdout_write(f"OK    ticket {ticket.pk}: short_description={summary!r}")
        count += 1
    stdout_write(f"DONE  described {count} ticket(s)")


class Command(TyperCommand):
    help: str = "Generate Ticket.short_description (#1156)."

    @command(name="describe")
    def describe(
        self,
        *,
        ticket_id: Annotated[int, typer.Option("--ticket-id", help="Describe this ticket only.")] = 0,
        all_missing: Annotated[
            bool,
            typer.Option("--all-missing", help="Backfill every ticket with a tracker title and no short_description."),
        ] = False,
    ) -> None:
        """Generate AI summaries for ticket(s)."""
        if ticket_id and all_missing:
            self.stdout.write("ERROR  pass exactly one of --ticket-id or --all-missing")
            raise SystemExit(2)
        if not ticket_id and not all_missing:
            self.stdout.write("ERROR  pass exactly one of --ticket-id or --all-missing")
            raise SystemExit(2)
        if all_missing:
            _describe_all_missing(stdout_write=self.stdout.write)
        else:
            describe_ticket(ticket_id, stdout_write=self.stdout.write)


__all__ = ["BaseCommand", "Command"]
