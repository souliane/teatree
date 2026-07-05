"""``t3 speak-dm`` — attach spoken audio to an already-posted user DM (#2171).

Top-level convenience over the ``speak_dm`` Django management command. The
question-mirror hook spawns it detached so the AskUserQuestion Slack DM carries
audio to the user's phone without blocking the hook. Delegates via
``call_command`` exactly like ``t3 speak``.
"""

import typer

from teatree.utils.django_bootstrap import ensure_django


def speak_dm(
    *,
    channel: str = typer.Option(..., "--channel", help="Slack DM channel id the audio attaches to."),
    text: str = typer.Option(..., "--text", help="Text to speak. Use '-' to read it from stdin."),
    thread_ts: str = typer.Option("", "--thread-ts", help="Thread the audio DM under this ts."),
    overlay: str = typer.Option("", "--overlay", help="Set T3_OVERLAY_NAME for the call (per-overlay Slack creds)."),
) -> None:
    """Attach spoken audio to a user DM per [teatree.speak] (no-op unless slack/local on)."""
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415 — deferred until ensure_django() has booted Django

    call_command("speak_dm", channel, text, thread_ts=thread_ts, overlay=overlay)
