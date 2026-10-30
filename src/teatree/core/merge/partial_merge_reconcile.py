"""Reopen a historical settled ticket when its issue has a verified remainder."""

from typing import TypedDict

from teatree.core.intake.factory_admission import payload_labels, resolve_umbrella_labels
from teatree.core.intake.umbrella import umbrella_reason
from teatree.core.issue_writes.issue_candidate_selection import description_of
from teatree.core.merge.partial_merge import (
    MergedPrReading,
    PrLink,
    capped_remainder_item,
    hold_for_remainder,
    preview_remainder_hold,
)
from teatree.core.merge.partial_merge_report import PartialMergeMatch, inspect_settled_ticket
from teatree.core.models import Ticket


class UnsettleResult(TypedDict, total=False):
    ticket_id: int
    state: str
    planning_task: int | None
    remainder: list[str]
    dry_run: bool
    error: str


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


def _inspected_match(ticket: Ticket) -> tuple[PartialMergeMatch | None, str]:
    inspection = inspect_settled_ticket(ticket)
    if inspection.match is None or inspection.issue is None:
        return None, inspection.reason or "ticket inspection failed"
    issue = inspection.issue
    if umbrella := umbrella_reason(
        body=description_of(issue),
        labels=payload_labels(issue),
        umbrella_labels=resolve_umbrella_labels(ticket.overlay),
    ):
        return None, f"umbrella issue: {umbrella}"
    return inspection.match, ""


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
    match, refusal = _inspected_match(ticket)
    if match is None:
        return {"error": refusal}
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
