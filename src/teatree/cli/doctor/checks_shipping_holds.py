"""Advisory scan of every reviewed ticket awaiting shipping evidence."""

import typer


def check_shipping_holds() -> bool:
    from teatree.core.models.shipping_hold import (  # noqa: PLC0415 — deferred: ORM import needs the app registry
        held_author_tickets,
        shipping_hold_of,
    )

    try:
        rows = [
            (ticket.pk, (shipping_hold_of(ticket) or {}).get("reason", "unknown")) for ticket in held_author_tickets()
        ]
    except Exception as exc:  # noqa: BLE001 — a doctor scan must report any read failure
        typer.echo(f"WARN  Shipping-hold scan UNVERIFIED: {exc!r}")
        return True
    for ticket_pk, reason in rows:
        typer.echo(f"WARN  ticket {ticket_pk}: shipping held — {reason}")
    return True
