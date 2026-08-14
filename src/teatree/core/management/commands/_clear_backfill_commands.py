"""``ticket backfill-clears`` / ``ticket reconcile-clears`` — the CLEAR-ledger recovery surfaces.

A mixin on the ``ticket`` :class:`~django_typer.management.TyperCommand` (django-typer
collects ``@command`` methods from every ``TyperCommand`` base in the MRO), delegating
to :mod:`teatree.core.merge.clear_backfill` and
:mod:`teatree.core.merge.clear_reconcile` so the walks live in the merge domain and the
commands stay surfaces.
"""

from typing import IO, Annotated, cast

import typer
from django.utils import timezone
from django_typer.management import TyperCommand, command

from teatree.core.machine_output import emit
from teatree.core.merge.clear_backfill import ClearBackfillRow, backfill_clear_tickets
from teatree.core.merge.clear_reconcile import reconcile_settled_clears


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

    @command()
    def reconcile_clears(
        self,
        *,
        dry_run: Annotated[bool, typer.Option(help="Show what would be consumed without persisting.")] = False,
        json_output: Annotated[bool, typer.Option("--json", help="Emit the report rows as JSON.")] = False,
    ) -> list[str]:
        """Consume every standing merge authorisation whose PR already merged or closed.

        A merge that lands outside the keystone (a lost post-hook, a hand-run merge)
        leaves the ``MergeClear`` unconsumed forever, so the backlog alarms on PRs that
        already landed. This asks the forge about each standing row and spends the ones
        it reports settled; a PR still open, or one whose state cannot be read, is left
        exactly as it was. No ``MergeAudit`` is written — that would claim keystone
        provenance for a merge the keystone did not execute.
        """
        from teatree.backends.loader import pr_open_state  # noqa: PLC0415 — deferred: keeps CLI startup light

        report = reconcile_settled_clears(read_state=pr_open_state, now=timezone.now(), dry_run=dry_run)
        lines = report.lines()
        self.print_result = False
        emit(
            report.settled,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human="".join(f"{line}\n" for line in lines),
        )
        return lines
