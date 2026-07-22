"""``manage.py ticket_short_describe`` — generate ``Ticket.short_description`` (#1156).

Two invocation forms over the summariser in :mod:`teatree.agents.short_describe`:
``--ticket-id <N>`` describes one ticket (reads ``extra["issue_title"]`` and writes the
generated summary back to ``ticket.short_description``); ``--all-missing`` backfills
every ticket with a non-blank ``extra["issue_title"]`` and a blank ``short_description``
(a one-shot CLI sweep after rollout, before the loop has scanned each ticket). The loop
itself reaches the same summariser through the ``short_describe`` deterministic phase
(:mod:`teatree.core.deterministic_dispatch`), not this command.
"""

from typing import Annotated

import typer
from django.core.management.base import BaseCommand
from django_typer.management import TyperCommand, command

from teatree.agents.short_describe import describe_all_missing, describe_ticket


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
            describe_all_missing(stdout_write=self.stdout.write)
        else:
            describe_ticket(ticket_id, stdout_write=self.stdout.write)


__all__ = ["BaseCommand", "Command"]
