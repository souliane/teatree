"""``t3 speak`` — read text aloud via the local text-to-speech seam (#1791).

Top-level convenience over the ``speak`` Django management command.
Anything that resolves config + the Slack backend runs through the
management framework (Django bootstrapped by it, not a manual
``django.setup()`` in a plain typer command) — so this delegates via
``call_command`` exactly like ``t3 cost``.
"""

import typer

from teatree.utils.django_bootstrap import ensure_django


def speak(
    text: str = typer.Argument(..., help="Text to read aloud. Use '-' to read it from stdin."),
    *,
    overlay: str = typer.Option("", "--overlay", help="Set T3_OVERLAY_NAME for the call (per-overlay Slack creds)."),
) -> None:
    """Read text aloud per the resolved speak_mode + speak_target (no-op when off)."""
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415

    call_command("speak", text, overlay=overlay)
