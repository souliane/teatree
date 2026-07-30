"""``ticket backfill-clears`` — the operator surface of the CLEAR ticket-link recovery.

A mixin on the ``ticket`` :class:`~django_typer.management.TyperCommand` (django-typer
collects ``@command`` methods from every ``TyperCommand`` base in the MRO), delegating
to :func:`teatree.core.merge.clear_backfill.backfill_clear_tickets` so the walk lives
in the merge domain and the command stays a surface.
"""

from typing import IO, Annotated, cast

import typer
from django_typer.management import TyperCommand, command

from teatree.core.machine_output import emit
from teatree.core.merge.clear_backfill import ClearBackfillRow, backfill_clear_tickets


class ClearBackfillCommands(TyperCommand):
    @command()
    def backfill_clears(
        self,
        *,
        dry_run: Annotated[bool, typer.Option(help="Show what would be linked without persisting.")] = False,
        json_output: Annotated[bool, typer.Option("--json", help="Emit the report rows as JSON.")] = False,
    ) -> list[ClearBackfillRow]:
        """Recover the ticket link on consumed CLEARs issued without ``--ticket-id``.

        Links each consumed, ticketless ``MergeClear`` to the ticket its PR belongs
        to, marks that ``PullRequest`` row MERGED, and reconciles the ticket toward
        MERGED where the CLEAR carries its own merge audit. Every row that cannot be
        resolved — and every live unconsumed CLEAR, which is left alone so its merge
        gates are unchanged — is reported, never silently skipped.
        """
        report = backfill_clear_tickets(dry_run=dry_run)
        rows = list(report.rows)
        self.print_result = False
        emit(
            rows,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human="".join(f"{line}\n" for line in report.lines()),
        )
        return rows
