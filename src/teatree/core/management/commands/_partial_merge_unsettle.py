"""CLI surface for historical partial-merge reconciliation."""

from typing import IO, Annotated, cast

import typer
from django_typer.management import TyperCommand, command

from teatree.core.machine_output import emit
from teatree.core.merge.partial_merge_reconcile import UnsettleResult, unsettle_ticket


class PartialMergeUnsettleCommands(TyperCommand):
    @command(name="unsettle", help="Reopen a settled ticket whose issue has an open remainder.")
    def unsettle(
        self,
        ticket_id: int,
        *,
        reason: Annotated[str, typer.Option(help="Name open work when the PR declared no remainder.")] = "",
        dry_run: Annotated[bool, typer.Option("--dry-run", help="Check without changing the ticket.")] = False,
        json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
    ) -> UnsettleResult:
        result = unsettle_ticket(ticket_id, reason=reason, dry_run=dry_run)
        self.print_result = False
        status = "would queue" if dry_run else "queued"
        human = (
            f"unsettle refused: {result['error']}"
            if "error" in result
            else f"ticket {ticket_id}: {status} planning for {', '.join(result['remainder'])}"
        )
        emit(
            result,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=human,
        )
        return result
