"""``t3 cost`` / ``t3 cost-profile`` — SDK-equivalent spend and where it goes.

Top-level convenience over the ``cost`` and ``cost_profile`` Django management commands. Anything
touching the ORM must run through the management framework (Django bootstrapped
by it, not a manual ``django.setup()`` in a plain typer command) — so this
delegates via ``call_command`` exactly like ``t3 loop tick``.
"""

import typer

from teatree.utils.django_bootstrap import ensure_django


def cost(
    *,
    json_output: bool = typer.Option(False, "--json", help="Emit the structured report as JSON."),
) -> None:
    """Show cycle-to-date SDK-equivalent spend vs the monthly credit."""
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415 — deferred: Django import at call time

    # The ``cost`` TyperCommand writes its own output through the machine-output
    # seam (JSON to stdout under ``--json``, the human view to stderr); call it
    # for the side effect.
    call_command("cost", json_output=json_output)


def cost_profile(
    *,
    json_output: bool = typer.Option(False, "--json", help="Emit the structured report as JSON."),
    since: str = typer.Option("", "--since", help="ISO-8601 lower bound on attempt start (default: every attempt)."),
) -> None:
    """Profile real dispatches: read share, resume-context cost and addressable upper bounds."""
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415 — deferred: Django import at call time

    call_command("cost_profile", json_output=json_output, since=since)
