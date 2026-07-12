"""The ticket reconciliation sweeps, factored out of ``ticket.py``.

``sync_completions`` (advance post-ship tickets whose upstream issue is done)
and ``reconcile_overlay`` (backfill ``overlay`` where attribution disagrees with
inference) — the two whole-table sweeps that reconcile ticket rows against an
external truth — live here as a :class:`SweepCommands` mixin the ``ticket``
:class:`~django_typer.management.TyperCommand` inherits from, so ``t3 <overlay>
ticket sync-completions`` / ``reconcile-overlay`` mount unchanged while their LOC
stays out of the (cap-bound) ``ticket.py`` god-module. django-typer collects
``@command`` methods from every ``TyperCommand`` base in the MRO.
"""

import contextlib
import logging
from typing import TYPE_CHECKING, Annotated, TypedDict

import typer
from django.db import transaction
from django_typer.management import TyperCommand, command

from teatree.backends.loader import get_code_host_for_url
from teatree.core.models import Ticket
from teatree.core.overlay_loader import get_all_overlays

if TYPE_CHECKING:
    from teatree.core.overlay import OverlayBase

logger = logging.getLogger(__name__)


class CompletionResult(TypedDict, total=False):
    ticket_id: int
    issue_url: str
    from_state: str
    to_state: str
    action: str
    error: str


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
    ) -> list[CompletionResult]:
        """Check post-ship tickets against upstream issues and advance completed ones.

        Walks tickets in shipped/in_review/merged states, calls the overlay's
        ``is_issue_done()`` for each, and transitions completed tickets toward
        delivered. Use ``--dry-run`` to preview without touching state.
        """
        completable_states = frozenset({"shipped", "in_review", "merged"})
        results: list[CompletionResult] = []

        for overlay_name, overlay in get_all_overlays().items():
            tickets = Ticket.objects.filter(
                state__in=completable_states,
                overlay=overlay_name,
            ).exclude(issue_url="")

            for ticket in tickets:
                if not _issue_is_done(overlay, ticket):
                    continue
                result = _complete_one_ticket(ticket, dry_run=dry_run)
                results.append(result)
                self.stdout.write(_completion_line(result))

        for line in _completion_summary_lines(results, dry_run=dry_run):
            self.stdout.write(line)
        return results

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


def _issue_is_done(overlay: "OverlayBase", ticket: Ticket) -> bool:
    """Whether *ticket*'s upstream issue is done — the eligibility gate for advancement.

    A missing host, an issue-fetch failure, an error payload, or an unfinished issue
    all skip the ticket. A fetch failure is logged, never fatal — it must not abort the sweep.
    """
    host = get_code_host_for_url(overlay, ticket.issue_url)
    if host is None:
        return False
    try:
        issue_data = host.get_issue(ticket.issue_url)
    except Exception:  # noqa: BLE001 — an issue-fetch failure skips the ticket, never aborts the sweep
        logger.warning("Failed to fetch issue for ticket %s (%s)", ticket.pk, ticket.issue_url)
        return False
    if not isinstance(issue_data, dict) or "error" in issue_data:
        return False
    return bool(overlay.is_issue_done(issue_data))


def _complete_one_ticket(ticket: Ticket, *, dry_run: bool) -> CompletionResult:
    """Advance one done ticket toward delivered, returning the recorded outcome.

    A gate-refused (or otherwise failing) FSM advance is caught and recorded as a
    ``refused`` result so the sweep CONTINUES to the next ticket — the whole-table
    sweep must never abort on one bad row (CFG-2). ``--dry-run`` records the intent.
    """
    from_state = ticket.state
    if dry_run:
        return CompletionResult(
            ticket_id=int(ticket.pk), issue_url=ticket.issue_url, from_state=from_state, action="would_complete"
        )
    try:
        _advance_ticket(ticket)
    except Exception as exc:  # noqa: BLE001 — a gate-refused / failed FSM advance skips this ticket, never aborts the sweep
        logger.warning("Failed to advance ticket %s (%s): %s", ticket.pk, ticket.issue_url, exc)
        # ``_advance_ticket`` commits up to three transitions in separate
        # ``atomic()`` blocks, so a mid-chain refusal leaves the earlier
        # transitions persisted. Reload the true landing state to report the
        # partial progress instead of the (possibly stale) starting state; a
        # vanished row must not abort the sweep either, so keep the in-memory state.
        with contextlib.suppress(Exception):
            ticket.refresh_from_db()
        return CompletionResult(
            ticket_id=int(ticket.pk),
            issue_url=ticket.issue_url,
            from_state=from_state,
            to_state=ticket.state,
            action="refused",
            error=str(exc),
        )
    return CompletionResult(
        ticket_id=int(ticket.pk),
        issue_url=ticket.issue_url,
        from_state=from_state,
        to_state=ticket.state,
        action="completed",
    )


def _completion_line(result: CompletionResult) -> str:
    """The per-ticket stdout line for one completion outcome."""
    pk, from_state = result["ticket_id"], result["from_state"]
    if result["action"] == "would_complete":
        return f"  [dry-run] #{pk} ({from_state}) → completed: {result['issue_url']}"
    if result["action"] == "refused":
        to_state = result.get("to_state")
        if to_state and to_state != from_state:
            return f"  #{pk} {from_state} → {to_state} refused: {result['error']}"
        return f"  #{pk} {from_state} → refused: {result['error']}"
    return f"  #{pk} {from_state} → {result['to_state']}: {result['issue_url']}"


def _completion_summary_lines(results: list[CompletionResult], *, dry_run: bool) -> list[str]:
    """The trailing summary — advanced count plus an explicit report of any refusals."""
    refused = [r for r in results if r.get("action") == "refused"]
    advanced = [r for r in results if r.get("action") != "refused"]
    lines: list[str] = []
    if not advanced:
        lines.append("No tickets to advance.")
    else:
        lines.append(f"\n{len(advanced)} ticket(s) {'would be' if dry_run else ''} advanced.")
    if refused:
        lines.append(f"{len(refused)} ticket(s) skipped (gate-refused or errored):")
        lines.extend(f"  #{r['ticket_id']} ({r['from_state']}): {r['error']}" for r in refused)
    return lines


def _advance_ticket(ticket: Ticket) -> None:
    """Walk the ticket through remaining FSM transitions toward delivered."""
    with transaction.atomic():
        if ticket.state == "shipped":
            ticket.request_review()
            ticket.save()
    with transaction.atomic():
        if ticket.state == "in_review":
            ticket.mark_merged()
            ticket.save()
    with transaction.atomic():
        if ticket.state == "merged":
            ticket.retrospect()
            ticket.save()
