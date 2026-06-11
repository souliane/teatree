"""``t3 cost`` — SDK-equivalent spend of the loop's detached headless Agent-SDK usage.

Top-level convenience over the ``cost`` Django management command. Anything
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

    from django.core.management import call_command  # noqa: PLC0415

    # The ``cost`` TyperCommand echoes its rendered output to stdout itself
    # (django-typer serialises the return value); call it for the side effect.
    call_command("cost", json_output=json_output)
