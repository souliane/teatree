"""``t3 <overlay> ticket bulk-close`` / ``integration-review-override`` — PR-08 close-flow gates.

Factored out of ``ticket.py`` as a :class:`CloseCommands` mixin (the module-health
LOC cap), exactly like ``RubricCommands`` / ``ContextCommands``: django-typer
collects ``@command`` methods from every ``TyperCommand`` base in the MRO, so
these mount under ``t3 <overlay> ticket bulk-close`` /
``integration-review-override`` with the CLI surface unchanged.

``bulk-close`` closes (``ignore``) a batch of tickets behind the no-bulk-close
guard (:func:`teatree.core.gates.bulk_close_gate.check_bulk_close`);
``integration-review-override`` records the audited escape hatch for the
cross-repo integration-review gate.
"""

from typing import Annotated, TypedDict

import typer
from django.db import transaction
from django_typer.management import TyperCommand, command

from teatree.core.models import Ticket


class BulkCloseResult(TypedDict, total=False):
    closed: bool
    closed_ids: list[int]
    refused: bool
    reason: str


class IntegrationReviewOverrideResult(TypedDict, total=False):
    ticket_id: int
    reason: str


class CloseCommands(TyperCommand):
    """Mixin holding the PR-08 close-flow commands."""

    @command(name="bulk-close")
    def bulk_close(
        self,
        *,
        ids: Annotated[str, typer.Option("--ids", help="Comma-separated ticket ids to close (ignore).")] = "",
        confirm: Annotated[
            str,
            typer.Option("--confirm", help="Comma-separated per-item confirmation tokens (each an id)."),
        ] = "",
    ) -> BulkCloseResult:
        """Close (``ignore``) a batch of tickets, gated by the no-bulk-close guard (PR-08).

        A batch of more than ``bulk_close_threshold`` tickets is refused unless
        every id is echoed in ``--confirm`` — so a mis-scoped sweep cannot
        mass-close silently. A batch at or under the threshold needs no
        confirmation.
        """
        from teatree.core.gates.bulk_close_gate import check_bulk_close  # noqa: PLC0415

        target_ids = [chunk.strip() for chunk in ids.split(",") if chunk.strip()]
        confirmed = [chunk.strip() for chunk in confirm.split(",") if chunk.strip()]
        if not target_ids:
            self.stderr.write("  bulk-close refused: --ids is required (comma-separated ticket ids)")
            raise SystemExit(1)

        refusal = check_bulk_close(items=target_ids, confirmed_tokens=confirmed)
        if refusal:
            self.stdout.write(f"  {refusal}")
            return {"refused": True, "reason": refusal}

        closed: list[int] = []
        with transaction.atomic():
            for raw_id in target_ids:
                ticket = self._resolve(int(raw_id))
                ticket.ignore()
                ticket.save()
                closed.append(int(ticket.pk))
        self.stdout.write(f"  closed {len(closed)} ticket(s): {', '.join(str(cid) for cid in closed)}")
        return {"closed": True, "closed_ids": closed}

    @command(name="integration-review-override")
    def integration_review_override(
        self,
        ticket_id: int,
        *,
        reason: Annotated[str, typer.Option("--reason", help="Why this >=2-repo ticket is exempt.")] = "",
    ) -> IntegrationReviewOverrideResult:
        """Record the audited escape hatch for the cross-repo integration-review gate (PR-08).

        Sets ``extra['integration_review_override']`` so ``mark_delivered`` lets a
        legitimately-exempt >=2-repo ticket close without an integration-review
        artifact. A blank reason is refused — the override must be attributable.
        """
        if not reason.strip():
            self.stderr.write("  integration-review-override refused: --reason is required")
            raise SystemExit(1)
        ticket = self._resolve(ticket_id)
        ticket.merge_extra(set_keys={"integration_review_override": {"reason": reason.strip()}})
        self.stdout.write(f"  recorded integration-review override for ticket {ticket_id}")
        return {"ticket_id": ticket_id, "reason": reason.strip()}

    def _resolve(self, ticket_id: int) -> Ticket:
        """Fetch a ticket or abort the subcommand with a nonzero exit."""
        try:
            return Ticket.objects.get(pk=ticket_id)
        except Ticket.DoesNotExist:
            self.stderr.write(f"  Ticket {ticket_id} not found")
            raise SystemExit(1) from None
