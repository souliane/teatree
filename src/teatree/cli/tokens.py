"""``t3 tokens`` — per-account Anthropic token health report.

Top-level convenience over the ``tokens`` Django management command. Anything
touching the ORM must run through the management framework (Django bootstrapped
by it, not a manual ``django.setup()`` in a plain typer command) — so this
delegates via ``call_command`` exactly like ``t3 cost``.
"""

import typer

from teatree.utils.django_bootstrap import ensure_django

_ADHOC_HELP = (
    "Ad-hoc Anthropic token to health-probe as an extra row (repeatable) — for checking a "
    "freshly-minted token before saving it. Warning: a token on the command line is visible "
    "in 'ps' output and your shell history."
)
_CACHED_HELP = (
    "Render the stored routing verdict instead of probing — the view of what the account "
    "selector believes. Values may be stale; the default report always probes live."
)
_PICK_HELP = (
    "Print the SELECTED OAuth account's pass entry path (never a token) and pin it, so an "
    "interactive shell rides the same routing a dispatched agent gets."
)
_SCOPE_HELP = "Routing scope to select for; defaults to the active overlay (T3_OVERLAY_NAME)."


def tokens(
    *,
    json_output: bool = typer.Option(False, "--json", help="Emit the structured report as JSON."),
    tokens: list[str] | None = typer.Option(None, "--token", help=_ADHOC_HELP),
    cached: bool = typer.Option(False, "--cached", help=_CACHED_HELP),
    pick: bool = typer.Option(False, "--pick", help=_PICK_HELP),
    scope: str = typer.Option("", "--scope", help=_SCOPE_HELP),
) -> None:
    """Show per-account Anthropic 5h / weekly token utilization + status."""
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415 — deferred: Django import at call time

    # The ``tokens`` TyperCommand writes its own output through the machine-output
    # seam (JSON to stdout under ``--json``, the human table to stderr); call it for
    # the side effect. The ad-hoc tokens ride a plain kwarg — never re-serialised
    # into an argv the classifier scans.
    call_command("tokens", json_output=json_output, tokens=tokens, cached=cached, pick=pick, scope=scope)
