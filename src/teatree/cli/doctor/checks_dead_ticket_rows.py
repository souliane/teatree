"""Tickets no forge query can reach must be NAMED, not discovered by hand (#4527).

Intake discovers candidates from author- and label-scoped forge queries, so a
``Ticket`` whose ``issue_url`` is blank — or is a synthetic loop anchor carrying no
description — is unreachable by construction. Fifty accumulated in the live control
DB before anyone looked, each the only surviving record of one owner request, and
every health surface stayed green throughout: an unfindable row is indistinguishable
from a row nobody has needed yet.

Advisory, never a gate. These rows describe requests an operator has to decide about
— re-file, or let go — not an invariant teatree broke, and a doctor FAIL that no
command can clear trains its reader to skip the whole report.

``Ticket`` carries no creation stamp, so the age is its oldest ``Task``'s. A row with
no task at all is still reported, without one: it is the most provably dead shape
there is, and hiding it because it lacks a timestamp would hide the worst case.
"""

from datetime import timedelta
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from teatree.core.models import Ticket

_LISTED = 10

#: A lane dispatched minutes ago has not failed to become work yet — it is still
#: running. Only a row that has had time to reach a work item and did not is evidence
#: of the drop this check reports.
_GRACE = timedelta(days=2)


def check_dead_ticket_rows() -> bool:
    """WARN once per non-terminal ticket intake could never find, oldest first."""
    from django.utils import timezone  # noqa: PLC0415 — deferred: needs Django configured

    from teatree.core.models import Ticket  # noqa: PLC0415 — deferred: ORM import needs the app registry

    now = timezone.now()
    try:
        rows = [
            (ticket, ticket.oldest_task)
            for ticket in Ticket.objects.unfindable()
            if ticket.oldest_task is None or now - ticket.oldest_task >= _GRACE
        ]
    except Exception as exc:  # noqa: BLE001 — a doctor check must never crash the run
        typer.echo(f"WARN  Dead-ticket-row scan UNVERIFIED: the ticket table could not be read ({exc!r}).")
        return True
    if not rows:
        return True
    typer.echo(
        f"WARN  {len(rows)} ticket row(s) carry nothing intake can find them by — no forge issue, no "
        "description. Each is the only surviving record of a request someone was told is tracked; "
        "nothing will ever claim them. Re-file the ones that still matter, then close the rest; "
        "`t3 <overlay> ticket dead-rows --json` lists every one."
    )
    for ticket, oldest in rows[:_LISTED]:
        typer.echo(f"      {_describe(ticket, age=_age(now - oldest) if oldest else 'no task')}")
    if len(rows) > _LISTED:
        typer.echo(f"      … and {len(rows) - _LISTED} more (`t3 <overlay> ticket dead-rows` lists every one).")
    return True


def _describe(ticket: "Ticket", *, age: str) -> str:
    """One row's line: how long it has sat, and the request text that is all it holds."""
    return f"ticket {ticket.pk} — {age} old, {ticket.state}: {ticket.recorded_request()[:120]}"


def _age(delta: timedelta) -> str:
    days = delta.days
    if days >= 1:
        return f"{days}d"
    return f"{delta.seconds // 3600}h"


__all__ = ["check_dead_ticket_rows"]
