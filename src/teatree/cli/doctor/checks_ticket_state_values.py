"""A Ticket value outside ``Ticket.State.values`` matches no transition source (#4779).

The rename carries no dual-read shim, so pre-rename code still running after migration
0093 can write a retired name — into ``state``, into the ``ignored_from`` /
``reopened_from`` snapshot ``unignore()`` assigns straight back into ``state``, or into
a ``TicketTransition`` endpoint. Such a row can never transition, no board column shows
it, and history splits its edges, so it fails the run.
"""

from collections.abc import Iterator

import typer

_LISTED = 10
_SNAPSHOT_KEYS = ("ignored_from", "reopened_from")


def _is_live(value: object, known: frozenset[str]) -> bool:
    return isinstance(value, str) and value in known


def _unknown_values(known: frozenset[str]) -> Iterator[str]:
    from teatree.core.models import (  # noqa: PLC0415 — deferred: ORM import needs the app registry
        Ticket,
        TicketTransition,
    )

    for pk, state in Ticket.objects.exclude(state__in=known).order_by("pk").values_list("pk", "state"):
        yield f"{pk} state={state!r}"
    for pk, extra in (
        Ticket.objects.filter(extra__has_any_keys=_SNAPSHOT_KEYS).order_by("pk").values_list("pk", "extra")
    ):
        for key in _SNAPSHOT_KEYS:
            if key in extra and not _is_live(extra[key], known):
                yield f"{pk} {key}={extra[key]!r}"
    for pk, from_state, to_state in (
        TicketTransition.objects.exclude(from_state__in=known, to_state__in=known)
        .order_by("pk")
        .values_list("pk", "from_state", "to_state")
    ):
        yield from (
            f"transition {pk} {field}={value!r}"
            for field, value in (("from_state", from_state), ("to_state", to_state))
            if value not in known
        )


def check_unknown_ticket_states() -> bool:
    """FAIL naming every ticket state, state snapshot or transition endpoint that is not a live ``Ticket.State``."""
    from teatree.core.models import Ticket  # noqa: PLC0415 — deferred: ORM import needs the app registry

    try:
        findings = list(_unknown_values(frozenset(Ticket.State.values)))
    except Exception as exc:  # noqa: BLE001 — a doctor check must never crash the run
        typer.echo(f"WARN  Unknown-ticket-state scan UNVERIFIED: the ticket tables could not be read ({exc!r}).")
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
