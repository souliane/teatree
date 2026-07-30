"""The ticket reconciliation sweeps, factored out of ``ticket.py``.

``sync_completions`` (the operator-facing surface of the board reconcile) and
``reconcile_overlay`` (backfill ``overlay`` where attribution disagrees with
inference) — the two whole-table sweeps that reconcile ticket rows against an
external truth — live here as a :class:`SweepCommands` mixin the ``ticket``
:class:`~django_typer.management.TyperCommand` inherits from, so ``t3 <overlay>
ticket sync-completions`` / ``reconcile-overlay`` mount unchanged while their LOC
stays out of the (cap-bound) ``ticket.py`` god-module. django-typer collects
``@command`` methods from every ``TyperCommand`` base in the MRO.

``sync_completions`` delegates to
:func:`teatree.loop.scanners.board_reconcile.reconcile_board`
rather than carrying its own walk, so the command a human types and the cadenced
scanner that runs unattended are literally the same reconciliation path (#3841).
"""

import logging
from typing import Annotated, TypedDict

import typer
from django_typer.management import TyperCommand, command

from teatree.core.models import Ticket
from teatree.loop.scanners.board_reconcile import DEFAULT_PROBE_BUDGET, BoardTransition, reconcile_board

logger = logging.getLogger(__name__)


class ReattributeResult(TypedDict, total=False):
    ticket_id: int
    issue_url: str
    from_overlay: str
    to_overlay: str
    action: str


class SweepCommands(TyperCommand):
    @command()
    def sync_completions(
        self,
        *,
        dry_run: Annotated[bool, typer.Option(help="Show what would transition without acting.")] = False,
        probe_budget: Annotated[int, typer.Option(help="Maximum forge reads this run may issue.")] = (
            DEFAULT_PROBE_BUDGET
        ),
    ) -> list[BoardTransition]:
        """Reconcile the ticket board against forge truth and advance what has landed.

        Advances a ticket whose PR merged (a linked ``PullRequest`` row, or the
        ticket's own ``issue_url`` the forge reports merged), resolves one whose PR
        closed unmerged, and walks a post-ship ticket whose upstream issue is done
        toward delivered. The same path the cadenced ``board_reconcile`` scanner
        runs, so the manual command and the loop can never disagree. Use
        ``--dry-run`` to preview the proposed transitions without touching state.
        """
        report = reconcile_board(dry_run=dry_run, probe_budget=probe_budget)
        for line in report.lines():
            self.stdout.write(line)
        return list(report.transitions)

    @command()
    def reconcile_overlay(
        self,
        *,
        dry_run: Annotated[bool, typer.Option(help="Show what would change without persisting.")] = False,
    ) -> list[ReattributeResult]:
        """Backfill ``overlay`` for rows whose attribution disagrees with inference.

        Walks every ticket with an ``issue_url`` and re-runs overlay
        inference (now routed through ``get_workspace_repos()``). Rows whose
        stored overlay differs from a *conclusive* inference are corrected;
        an inconclusive (empty) inference never blanks an existing value.
        Use ``--dry-run`` to preview.
        """
        results: list[ReattributeResult] = []

        for ticket in Ticket.objects.exclude(issue_url="").order_by("pk"):
            inferred = ticket._infer_overlay()  # noqa: SLF001 — backfill owns this model concern.
            if not inferred or inferred == ticket.overlay:
                continue

            from_overlay = ticket.overlay
            from_label = from_overlay or "(none)"
            if dry_run:
                results.append(
                    ReattributeResult(
                        ticket_id=int(ticket.pk),
                        issue_url=ticket.issue_url,
                        from_overlay=from_overlay,
                        to_overlay=inferred,
                        action="would_reattribute",
                    )
                )
                self.stdout.write(f"  [dry-run] #{ticket.pk}: {from_label} → {inferred}: {ticket.issue_url}")
            else:
                ticket.apply_inferred_overlay(inferred)
                results.append(
                    ReattributeResult(
                        ticket_id=int(ticket.pk),
                        issue_url=ticket.issue_url,
                        from_overlay=from_overlay,
                        to_overlay=ticket.overlay,
                        action="reattributed",
                    )
                )
                self.stdout.write(f"  #{ticket.pk}: {from_label} → {ticket.overlay}: {ticket.issue_url}")

        if not results:
            self.stdout.write("All ticket overlays already consistent with inference.")
        else:
            verb = "would be" if dry_run else "were"
            self.stdout.write(f"\n{len(results)} ticket(s) {verb} re-attributed.")
        return results
