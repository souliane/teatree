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
