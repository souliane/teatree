"""The explicit operator command for an audited merge void."""

from typing import Annotated, TypedDict

import typer
from django.core.management.base import CommandError
from django_typer.management import TyperCommand, command

from teatree.core.merge.void_merge import MergeVoidError, void_merge


class VoidMergeResult(TypedDict, total=False):
    void_id: int
    ticket_id: int
    pull_request_id: int
    ticket_state: str
    pull_request_state: str


class VoidMergeCommands(TyperCommand):
    @command(name="void-merge")
    def void_merge_command(
        self,
        pr: int,
        *,
        actor: Annotated[str, typer.Option(help="Operator identity for the audit.")],
        reason: Annotated[str, typer.Option(help="Why the recorded merge was false.")],
        prior_ticket_state: Annotated[
            str, typer.Option(help="Prior ticket state if its transition ledger is missing.")
        ] = "",
        prior_pr_state: Annotated[
            str, typer.Option(help="Prior PR state if it was merged before state tracking.")
        ] = "",
    ) -> VoidMergeResult:
        try:
            row = void_merge(
                pr_pk=pr,
                actor=actor,
                reason=reason,
                prior_ticket_state=prior_ticket_state,
                prior_pr_state=prior_pr_state,
            )
        except MergeVoidError as exc:
            raise CommandError(str(exc)) from exc
        result: VoidMergeResult = {
            "void_id": int(row.pk),
            "ticket_id": row.ticket_id,
            "pull_request_id": row.pull_request_id,
            "ticket_state": row.ticket_state_before_merge,
            "pull_request_state": row.pr_state_before_merge,
        }
        self.stdout.write(
            f"Voided merge for PR row {pr}; ticket {row.ticket_id} restored to {row.ticket_state_before_merge}"
        )
        return result
