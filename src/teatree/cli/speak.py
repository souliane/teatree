"""``t3 speak`` — read text aloud via the local text-to-speech seam (#2060).

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
    """Read text aloud through the local speakers per [teatree.speak] (no-op unless local = all)."""
    ensure_django()

    if _runtime_is_headless():
        typer.echo(
            "t3 speak is a local-audio-only sink and cannot reach an away user under "
            "agent_runtime=headless — route user contact through the needs_user_input → "
            "DeferredQuestion → Slack path instead. Nothing was spoken.",
            err=True,
        )
        return

    from django.core.management import call_command  # noqa: PLC0415 — deferred: Django import at call time

    call_command("speak", text, overlay=overlay)


def _runtime_is_headless() -> bool:
    from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: Django-dependent read at call time
    from teatree.config.agent_enums import AgentRuntime  # noqa: PLC0415 — deferred

    try:
        return get_effective_settings().agent_runtime is AgentRuntime.HEADLESS
    except Exception:  # noqa: BLE001 — a settings-read failure must never silence a present user's read
        return False
