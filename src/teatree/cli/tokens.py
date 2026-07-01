"""``t3 tokens`` — per-account Anthropic token health report.

Top-level convenience over the ``tokens`` Django management command. Anything
touching the ORM must run through the management framework (Django bootstrapped
by it, not a manual ``django.setup()`` in a plain typer command) — so this
delegates via ``call_command`` exactly like ``t3 cost``.
"""

import typer

from teatree.utils.django_bootstrap import ensure_django


def tokens(
    *,
    json_output: bool = typer.Option(False, "--json", help="Emit the structured report as JSON."),
) -> None:
    """Show per-account Anthropic 5h / weekly token utilization + status."""
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415

    # The ``tokens`` TyperCommand echoes its rendered output to stdout itself
    # (django-typer serialises the return value); call it for the side effect.
    call_command("tokens", json_output=json_output)
