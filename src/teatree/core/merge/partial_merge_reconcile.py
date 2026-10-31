"""Reopen a historical settled ticket when its issue has a verified remainder."""

from typing import TypedDict

from teatree.core.merge.partial_merge import (
    MergedPrReading,
    PrLink,
    capped_remainder_item,
    hold_for_remainder,
    preview_remainder_hold,
)
from teatree.core.merge.partial_merge_report import InspectionOutcome, PartialMergeMatch, inspect_settled_ticket
from teatree.core.models import Ticket


class UnsettleResult(TypedDict, total=False):
    ticket_id: int
    state: str
    planning_task: int | None
    remainder: list[str]
    dry_run: bool
    error: str
    unreadable: bool


def _candidate(ticket_id: int) -> tuple[Ticket | None, str]:
    try:
        ticket = Ticket.objects.get(pk=ticket_id)
    except Ticket.DoesNotExist:
        return None, f"Ticket {ticket_id} not found"
    if ticket.role != Ticket.Role.AUTHOR:
        return None, "only author tickets can be unsettled"
    if ticket.state not in Ticket.merged_states():
        return None, f"ticket {ticket_id} is not settled ({ticket.state})"
    if not ticket.issue_url.startswith(("https://", "http://")):
        return None, "ticket has no forge issue URL"
    return ticket, ""


def _remainder(match: PartialMergeMatch, reason: str) -> tuple[list[str], str]:
    if match["link"] == PrLink.CLOSING:
        return [], "the merged pull request used a closing keyword for this issue"
    remainder = [*match["unmet"]]
    if normalized_reason := capped_remainder_item(" ".join(reason.split())):
        remainder.append(normalized_reason)
    if not remainder:
        return [], "no remainder evidence; pass --reason to name the open work"
    return remainder, ""


def unsettle_ticket(ticket_id: int, *, reason: str, dry_run: bool) -> UnsettleResult:
    ticket, refusal = _candidate(ticket_id)
    if ticket is None:
        return {"error": refusal}
    inspection = inspect_settled_ticket(ticket)
    match = inspection.match
    if match is None or inspection.issue is None:
        reason_text = inspection.reason or "ticket inspection failed"
        return {"error": reason_text, "unreadable": inspection.outcome is InspectionOutcome.UNREADABLE}
    if umbrella := inspection.issue.umbrella(ticket):
        return {"error": f"umbrella issue: {umbrella}"}
    remainder, refusal = _remainder(match, reason)
    if refusal:
        return {"error": refusal}
    reading = MergedPrReading(PrLink(match["link"]), tuple(remainder))
    if dry_run:
        task = None
        state = preview_remainder_hold(ticket, reading)
    else:
        task = hold_for_remainder(ticket, reading, merged_pr=match["pr_url"], settled=True)
        if task is not None:
            ticket.refresh_from_db()
        state = ticket.state if task is not None else None
    if state is None:
        return {"error": "ticket could not be reopened or this remainder was already planned"}
    return {
        "ticket_id": ticket_id,
        "state": state,
        "planning_task": task.pk if task else None,
        "remainder": remainder,
        "dry_run": dry_run,
    }
