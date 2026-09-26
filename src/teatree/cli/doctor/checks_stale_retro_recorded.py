"""RETRO_RECORDED is neither settled nor progressing — a ticket stuck there is silent (#4779).

``_SETTLED_STATES`` deliberately excludes RETRO_RECORDED: a MERGED PR is not yet
DELIVERED once ``retrospect()`` fires, so the ticket is genuinely still in flight.
But nothing else watches it either — the retro worker (``execute_retrospect``)
drives ``mark_delivered()`` on success, and a failed or never-dispatched retro
leaves the ticket parked with no forward path and no `_SETTLED_STATES` coverage
to flag it. Advisory, never a gate, matching ``check_dead_ticket_rows``'s posture.
"""

from datetime import timedelta

import typer

_LISTED = 10

#: A ticket that just recorded its retro has not failed to deliver yet — it is still running.
#: Only one that has had time to reach `mark_delivered()` and did not is evidence
#: of the stall this check reports.
_GRACE = timedelta(days=2)


def check_stale_retro_recorded() -> bool:
    """WARN once per RETRO_RECORDED ticket aged past `_GRACE`, oldest first."""
    from django.db.models import Max  # noqa: PLC0415 — deferred: needs Django configured
    from django.utils import timezone  # noqa: PLC0415 — deferred: needs Django configured

    from teatree.core.models import Ticket  # noqa: PLC0415 — deferred: ORM import needs the app registry

    now = timezone.now()
    try:
        rows = [
            (ticket, ticket.newest_task)
            for ticket in Ticket.objects.filter(state=Ticket.State.RETRO_RECORDED).annotate(
                newest_task=Max("tasks__created_at"),
            )
            if ticket.newest_task is None or now - ticket.newest_task >= _GRACE
        ]
    except Exception as exc:  # noqa: BLE001 — a doctor check must never crash the run
        typer.echo(f"WARN  Stale-retro-recorded scan UNVERIFIED: the ticket table could not be read ({exc!r}).")
        return True
    if not rows:
        return True
    typer.echo(
        f"WARN  {len(rows)} ticket(s) sat in RETRO_RECORDED past the retro worker's expected window — "
        "the merge landed but `mark_delivered()` never fired. Re-dispatch the retro phase or "
        "investigate `execute_retrospect` for each: "
        f"{', '.join(str(t.pk) for t, _ in rows[:_LISTED])}"
        f"{f' … and {len(rows) - _LISTED} more' if len(rows) > _LISTED else ''}."
    )
    return True


__all__ = ["check_stale_retro_recorded"]
