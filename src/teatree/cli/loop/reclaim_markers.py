"""``t3 loop reclaim-markers`` — clear orphaned issue-implementer markers on demand (#3275).

The sanctioned way to unjam intake: raw SQL against the ledger is (correctly)
blocked by the auto-mode classifier, so an operator whose ``issue_implementer``
budget is stranded by orphaned ``dispatched`` markers had no CLI to free it. This
wraps :meth:`ImplementedIssueMarker.objects.reconcile_stale` — the same
retroactive path the loop runs each tick — so the budget can be freed by hand.

Every grace the manager takes is exposed as an option. Without them the command
could only ever reproduce the tick's own verdict, so an operator staring at a
claim stranded minutes ago had to wait out the six-hour grace with no way to say
"I can see it is dead, release it now" — leaving raw SQL as the only lever, which
is exactly what this command exists to replace. Zero is their floor: a negative
grace moves the cutoff into the FUTURE, so it releases claims that are still in
flight and re-dispatches the very issues this ledger exists to guard.

Split out of ``teatree.cli.loop.app`` (module-health cap, same rationale as the
sibling ``claim_next`` / ``slack_answer`` splits) and registered flat on
``loop_app`` by that module.
"""

import datetime as dt

import typer

from teatree.utils.django_bootstrap import ensure_django


def reclaim_markers_command(
    *,
    overlay: str = typer.Option(
        "",
        "--overlay",
        help="Restrict to one overlay (default: reconcile every overlay's markers).",
    ),
    orphan_grace_hours: float | None = typer.Option(
        None,
        "--orphan-grace-hours",
        min=0,
        help="How long a ticket-less claim may linger before it is abandoned (default: 6). "
        "Pass 0 to free a claim stranded moments ago rather than waiting out the grace.",
    ),
    stall_grace_hours: float | None = typer.Option(
        None,
        "--stall-grace-hours",
        min=0,
        help="How long a claim whose ticket stopped moving may hold its slot while an open PR "
        "proves it is still mid-flight (default: 24).",
    ),
    dead_grace_hours: float | None = typer.Option(
        None,
        "--dead-grace-hours",
        min=0,
        help="How long a claim whose ticket has nothing queued and no open PR may hold its slot "
        "(default: 2). The attempt is over rather than slow, so it is not held to the stall grace.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit the reconcile result as JSON."),
) -> None:
    """Release non-terminal markers whose ticket is terminal, gone, or stalled, freeing intake budget."""
    ensure_django()

    from teatree.core.models import ImplementedIssueMarker  # noqa: PLC0415 — ORM import needs the app registry

    result = ImplementedIssueMarker.objects.reconcile_stale(
        overlay,
        orphan_grace=None if orphan_grace_hours is None else dt.timedelta(hours=orphan_grace_hours),
        stall_grace=None if stall_grace_hours is None else dt.timedelta(hours=stall_grace_hours),
        dead_grace=None if dead_grace_hours is None else dt.timedelta(hours=dead_grace_hours),
    )
    if json_output:
        import json  # noqa: PLC0415 — deferred: only the JSON path needs it

        typer.echo(
            json.dumps(
                {
                    "overlay": overlay,
                    "released": result.released,
                    "completed": list(result.completed),
                    "abandoned": list(result.abandoned),
                }
            )
        )
        return
    scope = f"overlay {overlay!r}" if overlay else "all overlays"
    typer.echo(
        f"Reclaimed {result.released} stale issue-marker(s) for {scope}: "
        f"{len(result.completed)} completed (terminal ticket or merged PR), "
        f"{len(result.abandoned)} abandoned (gone or stalled ticket)."
    )
