"""A Ticket value outside ``Ticket.State.values`` matches no transition source (#4779).

The rename carries no dual-read shim, so pre-rename code still running after migration
0093 can write a retired name — into ``state``, or into the ``ignored_from`` /
``reopened_from`` snapshot ``unignore()`` assigns straight back into ``state``. Such a
row can never transition and no board column shows it, so it fails the run.
"""

import typer

_LISTED = 10
_SNAPSHOT_KEYS = ("ignored_from", "reopened_from")


def check_unknown_ticket_states() -> bool:
    """FAIL naming every ticket whose state or state snapshot is not a live ``Ticket.State`` value."""
    from teatree.core.models import Ticket  # noqa: PLC0415 — deferred: ORM import needs the app registry

    known = set(Ticket.State.values)
    try:
        findings = [
            f"{pk} state={state!r}"
            for pk, state in Ticket.objects.exclude(state__in=known).order_by("pk").values_list("pk", "state")
        ]
        findings += [
            f"{pk} {key}={value!r}"
            for pk, extra in Ticket.objects.filter(extra__has_any_keys=_SNAPSHOT_KEYS)
            .order_by("pk")
            .values_list("pk", "extra")
            for key in _SNAPSHOT_KEYS
            if (value := extra.get(key)) is not None and value not in known
        ]
    except Exception as exc:  # noqa: BLE001 — a doctor check must never crash the run
        typer.echo(f"WARN  Unknown-ticket-state scan UNVERIFIED: the ticket table could not be read ({exc!r}).")
        return True
    if not findings:
        return True
    typer.echo(
        f"FAIL  {len(findings)} ticket value(s) name no live Ticket.State — no transition accepts them and "
        "the board hides them. Rewrite each to its renamed value (migration 0093's mapping): "
        f"{', '.join(findings[:_LISTED])}"
        f"{f' … and {len(findings) - _LISTED} more' if len(findings) > _LISTED else ''}."
    )
    return False


__all__ = ["check_unknown_ticket_states"]
