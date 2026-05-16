"""Ticket state management: transitions and listing for the loop and CLI."""

import logging
from typing import Annotated, TypedDict

import typer
from django.db import transaction
from django_fsm import TransitionNotAllowed
from django_typer.management import TyperCommand, command

from teatree.core.models import Ticket


class CompletionResult(TypedDict, total=False):
    ticket_id: int
    issue_url: str
    from_state: str
    to_state: str
    action: str


class CommentResult(TypedDict, total=False):
    issue_url: str
    comment_id: int
    error: str


class ReattributeResult(TypedDict, total=False):
    ticket_id: int
    issue_url: str
    from_overlay: str
    to_overlay: str
    action: str


logger = logging.getLogger(__name__)

_ALLOWED_TRANSITIONS = {
    "scope",
    "start",
    "code",
    "test",
    "review",
    "ship",
    "request_review",
    "mark_merged",
    "retrospect",
    "mark_delivered",
    "rework",
}


class Command(TyperCommand):
    @command()
    def transition(self, ticket_id: int, transition_name: str) -> dict[str, object]:
        """Transition a ticket to a new state.

        Accepts any of the allowed transition names: scope, start, code, test,
        review, ship, request_review, mark_merged, retrospect, mark_delivered, rework.
        """
        if transition_name not in _ALLOWED_TRANSITIONS:
            return {"error": f"Unknown transition: {transition_name}"}

        try:
            ticket = Ticket.objects.get(pk=ticket_id)
        except Ticket.DoesNotExist:
            return {"error": f"Ticket {ticket_id} not found"}

        method = getattr(ticket, transition_name, None)
        if method is None:
            return {"error": f"Invalid transition: {transition_name}"}

        try:
            with transaction.atomic():
                method()
                ticket.save()
        except TransitionNotAllowed:
            return {
                "error": f"Transition '{transition_name}' not allowed from state '{ticket.state}'",
            }

        return {"ticket_id": int(ticket.pk), "state": ticket.state}

    @command()
    def comment(
        self,
        issue_url: str,
        *,
        body: Annotated[str, typer.Option(help="Comment body text.")] = "",
        body_file: Annotated[str, typer.Option(help="Path to a file containing the comment body.")] = "",
    ) -> CommentResult:
        """Post a comment to an issue or work item by its URL.

        Resolves the code host per-URL across all registered overlays, so it
        works for any tracker an overlay is configured for (GitLab issues and
        work items, GitHub issues). Pass the body inline with ``--body`` or
        from a file with ``--body-file``.
        """
        from pathlib import Path  # noqa: PLC0415

        from teatree.backends.loader import get_code_host_for_url  # noqa: PLC0415
        from teatree.core.overlay_loader import get_all_overlays  # noqa: PLC0415

        text = Path(body_file).read_text(encoding="utf-8") if body_file else body
        if not text:
            return {"error": "No comment body: pass --body or --body-file"}

        for overlay in get_all_overlays().values():
            host = get_code_host_for_url(overlay, issue_url)
            if host is None:
                continue
            raw = host.post_issue_comment(issue_url=issue_url, body=text)
            error = raw.get("error") if isinstance(raw, dict) else None
            if error:
                self.stdout.write(f"  failed: {error}")
                return {"error": str(error)}
            comment_id = raw.get("id") if isinstance(raw, dict) else None
            self.stdout.write(f"  commented on {issue_url}")
            return {
                "issue_url": issue_url,
                "comment_id": comment_id if isinstance(comment_id, int) else 0,
            }

        return {"error": f"No code host could be resolved for {issue_url}"}

    @command(name="list")
    def list_tickets(self, state: str = "", overlay: str = "") -> list[dict[str, object]]:
        """List tickets, optionally filtered by state and/or overlay."""
        qs = Ticket.objects.order_by("-pk")
        if state:
            qs = qs.filter(state=state)
        if overlay:
            qs = qs.filter(overlay=overlay)
        return [
            {
                "id": int(ticket.pk),
                "state": ticket.state,
                "overlay": ticket.overlay,
                "issue_url": ticket.issue_url,
                "variant": ticket.variant,
            }
            for ticket in qs
        ]

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
        from teatree.backends.loader import get_code_host_for_url  # noqa: PLC0415
        from teatree.core.overlay_loader import get_all_overlays  # noqa: PLC0415

        completable_states = frozenset({"shipped", "in_review", "merged"})
        results: list[CompletionResult] = []

        for overlay_name, overlay in get_all_overlays().items():
            tickets = Ticket.objects.filter(
                state__in=completable_states,
                overlay=overlay_name,
            ).exclude(issue_url="")

            for ticket in tickets:
                host = get_code_host_for_url(overlay, ticket.issue_url)
                if host is None:
                    continue
                try:
                    issue_data = host.get_issue(ticket.issue_url)
                except Exception:  # noqa: BLE001
                    logger.warning("Failed to fetch issue for ticket %s (%s)", ticket.pk, ticket.issue_url)
                    continue
                if not isinstance(issue_data, dict) or "error" in issue_data:
                    continue
                if not overlay.is_issue_done(issue_data):
                    continue

                from_state = ticket.state
                if dry_run:
                    results.append(
                        CompletionResult(
                            ticket_id=int(ticket.pk),
                            issue_url=ticket.issue_url,
                            from_state=from_state,
                            action="would_complete",
                        )
                    )
                    self.stdout.write(f"  [dry-run] #{ticket.pk} ({from_state}) → completed: {ticket.issue_url}")
                else:
                    _advance_ticket(ticket)
                    results.append(
                        CompletionResult(
                            ticket_id=int(ticket.pk),
                            issue_url=ticket.issue_url,
                            from_state=from_state,
                            to_state=ticket.state,
                            action="completed",
                        )
                    )
                    self.stdout.write(f"  #{ticket.pk} {from_state} → {ticket.state}: {ticket.issue_url}")

        if not results:
            self.stdout.write("No tickets to advance.")
        else:
            self.stdout.write(f"\n{len(results)} ticket(s) {'would be' if dry_run else ''} advanced.")
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
                self.stdout.write(f"  [dry-run] #{ticket.pk}: {from_overlay or '∅'} → {inferred}: {ticket.issue_url}")
            else:
                ticket.reconcile_overlay()
                results.append(
                    ReattributeResult(
                        ticket_id=int(ticket.pk),
                        issue_url=ticket.issue_url,
                        from_overlay=from_overlay,
                        to_overlay=ticket.overlay,
                        action="reattributed",
                    )
                )
                self.stdout.write(f"  #{ticket.pk}: {from_overlay or '∅'} → {ticket.overlay}: {ticket.issue_url}")

        if not results:
            self.stdout.write("All ticket overlays already consistent with inference.")
        else:
            verb = "would be" if dry_run else "were"
            self.stdout.write(f"\n{len(results)} ticket(s) {verb} re-attributed.")
        return results


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
