"""``t3 loop reclaim-markers`` — clear orphaned issue-implementer markers on demand (#3275).

The sanctioned way to unjam intake: raw SQL against the ledger is (correctly)
blocked by the auto-mode classifier, so an operator whose ``issue_implementer``
budget is stranded by orphaned ``dispatched`` markers had no CLI to free it. This
wraps :meth:`ImplementedIssueMarker.objects.reconcile_stale` — the same
retroactive path the loop runs each tick — so the budget can be freed by hand.

Split out of ``teatree.cli.loop.app`` (module-health cap, same rationale as the
sibling ``claim_next`` / ``slack_answer`` splits) and registered flat on
``loop_app`` by that module.
"""

import typer

from teatree.utils.django_bootstrap import ensure_django


def reclaim_markers_command(
    *,
    overlay: str = typer.Option(
        "",
        "--overlay",
        help="Restrict to one overlay (default: reconcile every overlay's markers).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit the reconcile result as JSON."),
) -> None:
    """Release orphaned non-terminal markers whose ticket is terminal/gone, freeing intake budget."""
    ensure_django()

    from teatree.core.models import ImplementedIssueMarker  # noqa: PLC0415 — ORM import needs the app registry

    result = ImplementedIssueMarker.objects.reconcile_stale(overlay)
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
        f"{len(result.completed)} completed (terminal ticket), {len(result.abandoned)} abandoned (gone ticket)."
    )
