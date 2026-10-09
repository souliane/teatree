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
from typing import IO, Annotated, TypedDict, cast

import typer
from django_typer.management import TyperCommand, command

from teatree.core.machine_output import emit
from teatree.core.models import Ticket, TicketSweepRun
from teatree.loop.scanners.board_reconcile import DEFAULT_PROBE_BUDGET, reconcile_board
from teatree.loop.scanners.board_reconcile_report import BoardTransition

logger = logging.getLogger(__name__)


class SweepRunResult(TypedDict, total=False):
    run_id: str
    source: str
    changed_count: int
    examined_count: int
    external_skipped_count: int
    finished: bool
    error: str


class SweepTrendResult(TypedDict, total=False):
    latest_changed_count: int
    recent_changed_counts: list[int]
    zero_streak: int
    incomplete_runs: list[str]


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

    @command(name="sweep-begin")
    def sweep_begin(
        self,
        *,
        source: Annotated[str, typer.Option(help="Who is sweeping: interactive or loop.")] = "interactive",
        overlay: Annotated[str, typer.Option(help="Overlay this sweep covers.")] = "",
        json_output: Annotated[bool, typer.Option("--json", help="Emit the typed result as JSON on stdout.")] = False,
    ) -> None:
        """Open a ticket-hygiene sweep run and print its id (#162 Rule 4).

        Pass the printed ``run_id`` to every ``ticket comment`` the sweep makes,
        so each fold is attributed and the changed-ticket count is measured
        rather than reported. The run also makes the sweep's zero-comment
        invariant enforceable: with a run id set, every comment purpose is
        refused below the skill.
        """
        payload: SweepRunResult
        try:
            run = TicketSweepRun.objects.begin(source=source, overlay=overlay)
        except ValueError as exc:
            payload = {"error": str(exc)}
            human = ""
        else:
            payload = {"run_id": run.run_id, "source": run.source, "finished": False}
            human = run.run_id

        self.print_result = False
        emit(
            payload,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=human,
        )
        if "error" in payload:
            raise SystemExit(1)

    @command(name="sweep-finish")
    def sweep_finish(
        self,
        run_id: str,
        *,
        examined: Annotated[int, typer.Option(help="How many tickets the sweep looked at.")] = 0,
        external_skipped: Annotated[int, typer.Option(help="Tickets skipped as externally authored.")] = 0,
        json_output: Annotated[bool, typer.Option("--json", help="Emit the typed result as JSON on stdout.")] = False,
    ) -> None:
        """Close a sweep run, persisting its changed-ticket count — zero included.

        A sweep that changed nothing MUST still finish: without the row, a
        healthy factory and a sweep that never ran are indistinguishable, and
        the trend rule 4 asks for cannot be read.
        """
        payload: SweepRunResult
        try:
            run = TicketSweepRun.objects.finish(
                run_id=run_id, examined_count=examined, external_skipped_count=external_skipped
            )
        except (TicketSweepRun.DoesNotExist, ValueError) as exc:
            payload = {"error": f"sweep-finish refused: {exc}"}
            human = ""
        else:
            payload = {
                "run_id": run.run_id,
                "source": run.source,
                "changed_count": run.changed_count,
                "examined_count": run.examined_count,
                "external_skipped_count": run.external_skipped_count,
                "finished": True,
            }
            human = f"  run {run.run_id}: changed {run.changed_count} of {run.examined_count} examined"

        self.print_result = False
        emit(
            payload,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=human,
        )
        if "error" in payload:
            raise SystemExit(1)

    @command(name="sweep-trend")
    def sweep_trend(
        self,
        *,
        limit: Annotated[int, typer.Option(help="How many finished runs to report.")] = 10,
        json_output: Annotated[bool, typer.Option("--json", help="Emit the typed result as JSON on stdout.")] = False,
    ) -> None:
        """Report the changed-ticket count series, the zero streak, and any unfinished runs."""
        recent = list(TicketSweepRun.objects.recent(limit=limit))
        counts = [run.changed_count for run in recent]
        incomplete = [run.run_id for run in TicketSweepRun.objects.incomplete()]
        zero_streak = TicketSweepRun.objects.zero_streak()
        payload: SweepTrendResult = {
            "latest_changed_count": counts[0] if counts else 0,
            "recent_changed_counts": counts,
            "zero_streak": zero_streak,
            "incomplete_runs": incomplete,
        }

        def _human(stream: IO[str]) -> None:
            stream.write(f"  recent changed counts (newest first): {counts or '(none)'}\n")
            stream.write(f"  zero streak: {zero_streak}\n")
            if incomplete:
                stream.write(f"  unfinished runs: {', '.join(incomplete)}\n")

        self.print_result = False
        emit(
            payload,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=_human,
        )

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
