"""``t3 speak`` — the local text-to-speech seam (#2060), refused for user contact.

Local audio is a sink nobody is sitting in front of, so it can never reach the
user: teatree runs headless, and contact routes through ``needs_user_input`` →
``DeferredQuestion`` → Slack. The command stays so the refusal is stated rather
than the text vanishing into a box no one is at.
"""

import typer

from teatree.utils.django_bootstrap import ensure_django

_REFUSAL = (
    "t3 speak is a local-audio-only sink and cannot reach the user — route user contact "
    "through the needs_user_input → DeferredQuestion → Slack path instead. Nothing was spoken."
)


def speak(
    text: str = typer.Argument(..., help="Text to read aloud. Use '-' to read it from stdin."),
    *,
    overlay: str = typer.Option("", "--overlay", help="Set T3_OVERLAY_NAME for the call (per-overlay Slack creds)."),
) -> None:
    """Refuse to speak — local audio cannot reach the user."""
    del text, overlay
    ensure_django()
    typer.echo(_REFUSAL, err=True)
