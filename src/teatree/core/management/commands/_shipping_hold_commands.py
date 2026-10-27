"""Operator entry point for the held-review settlement pass."""

from typing import IO, Annotated, TypedDict, cast

import typer
from django_typer.management import TyperCommand, command

from teatree.core import managers_task_claim
from teatree.core.machine_output import emit
from teatree.core.models.shipping_hold import held_author_tickets
from teatree.loop.shipping_hold_settle import SettlementPassError, settle_held_tickets


class SettlementRow(TypedDict):
    ticket_id: int
    action: str
    reason: str


class ShippingHoldCommands(TyperCommand):
    @command(name="settle-holds")
    def settle_holds(
        self,
        *,
        dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
        json_output: Annotated[bool, typer.Option("--json")] = False,
    ) -> list[SettlementRow]:
        """Check held no-diff reviews; --dry-run reads without writing."""
        failure = ""
        try:
            results = settle_held_tickets(dry_run=dry_run)
        except SettlementPassError as exc:
            results = exc.applied
            failure = str(exc)
        rows: list[SettlementRow] = [
            {"ticket_id": result.ticket_pk, "action": result.action, "reason": result.reason} for result in results
        ]
        self.print_result = False
        remaining = held_author_tickets().count()
        if (
            not rows
            and remaining
            and not dry_run
            and not failure
            and (refusal := managers_task_claim.claim_admission_block_reason())
        ):
            failure = f"settlement deferred: {refusal}"
        if failure:
            cast("IO[str]", self.stderr).write(failure)
        emit(
            rows,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=f"{len(rows)} held ticket(s) checked; {remaining} held remaining",
        )
        return rows
