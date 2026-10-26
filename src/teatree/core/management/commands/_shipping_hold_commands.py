"""Operator entry point for the held-review settlement pass."""

from typing import IO, Annotated, TypedDict, cast

import typer
from django_typer.management import TyperCommand, command

from teatree.core.machine_output import emit
from teatree.loop.shipping_hold_settle import settle_held_tickets


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
        rows: list[SettlementRow] = [
            {"ticket_id": result.ticket_pk, "action": result.action, "reason": result.reason}
            for result in settle_held_tickets(dry_run=dry_run)
        ]
        self.print_result = False
        emit(
            rows,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=f"{len(rows)} held ticket(s) checked",
        )
        return rows
